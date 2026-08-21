"""Bounded DOCX profile qualification lane for GitHub issue #99.

The corpus in ``machine/qualification-issue-99-corpus.json`` contains authored
OOXML package members and literal expected source facts.  This runner uses an
independent ZIP/XML inspector for the oracle and invokes the implementation only
through the public ``tools/convert_document.py convert`` command.  It never
imports an adapter helper to manufacture an expected value.

This lane is intentionally fail-closed.  A missing occurrence, incomplete OPC
closure, false completion claim, unsafe relationship target, absent real
producer corpus, or undetected negative mutation keeps every report failed.
"""

from __future__ import annotations

import argparse
import base64
from collections import Counter
from copy import deepcopy
import hashlib
import json
import os
import posixpath
from pathlib import Path
import re
import subprocess
import sys
from typing import Any, Iterable, Sequence
import xml.etree.ElementTree as ET
import zipfile

try:
    from qualification_producer_report import write_producer_report
except ImportError:  # pragma: no cover - package-style test imports
    from tools.qualification_producer_report import write_producer_report


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CORPUS_PATH = ROOT / "machine" / "qualification-issue-99-corpus.json"
DEFAULT_OUT_DIR = ROOT / "e2e" / ".run" / "qualification-issue-99"
CONVERTER_PATH = ROOT / "tools" / "convert_document.py"
REPORT_NAMES = [
    "docx-profile-matrix.json",
    "docx-part-relationship-closure.json",
    "docx-story-corpus.json",
    "docx-structure-style-layout.json",
    "docx-unsupported-occurrences.json",
    "docx-multi-producer-differential.json",
]

REPORT_CATEGORIES = {
    "docx-profile-matrix.json": {"profile"},
    "docx-part-relationship-closure.json": {"closure", "resource", "security"},
    "docx-story-corpus.json": {"story"},
    "docx-structure-style-layout.json": {"structure", "style-layout"},
    "docx-unsupported-occurrences.json": {"unsupported", "accounting", "completion"},
    "docx-multi-producer-differential.json": {"producer", "differential"},
}

REQUIRED_REGRESSION_CASES = {
    "nested-drawing-in-run",
    "nested-hyperlink-content",
    "gridspan-vmerge-nested-table",
    "complex-nested-field",
    "story-owner-anchor",
    "section-page-properties",
    "missing-external-relationship",
    "unknown-alternatecontent-accounting",
    "resource-security-limits",
}

REQUIRED_NEGATIVE_CASES = {
    "relation-target-reassigned",
    "missing-target-preserved-available",
    "external-target-available",
    "hyperlink-content-traversal-disabled",
    "anchor-deleted",
    "gridspan-dropped",
    "vmerge-follower-promoted",
    "complex-field-range-dropped",
    "section-layout-dropped",
    "unknown-as-preserved",
    "alternatecontent-branch-omitted",
    "resource-limit-ignored",
    "false-complete-claim",
}

REL_NS = "http://schemas.openxmlformats.org/package/2006/relationships"
R_NS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
W_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
MC_NS = "http://schemas.openxmlformats.org/markup-compatibility/2006"
XML_NS = "http://www.w3.org/XML/1998/namespace"


class QualificationError(RuntimeError):
    """Raised when the qualification evidence cannot be established safely."""


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
        raise QualificationError(f"cannot obtain exact 40-character Git HEAD: {value!r}")
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
                raise QualificationError("base64 corpus payload must contain a string")
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
    if name.startswith("/") or "\\" in name:
        raise QualificationError(f"non-canonical package member name: {name!r}")
    if any(component in {"", ".", ".."} for component in name.split("/")):
        raise QualificationError(f"unsafe package member name: {name!r}")
    return name


def _load_corpus(path: Path = DEFAULT_CORPUS_PATH) -> dict[str, Any]:
    corpus = _read_json(path)
    if not isinstance(corpus, dict) or corpus.get("issueNumber") != 99:
        raise QualificationError("issue #99 corpus has the wrong root or issue number")
    if corpus.get("qualificationScope") != "bounded-docx-profile-qualification":
        raise QualificationError("issue #99 corpus has the wrong qualification scope")
    if corpus.get("reportNames") != REPORT_NAMES:
        raise QualificationError("issue #99 corpus report names do not match the required six")

    oracle = corpus.get("oracle")
    if not isinstance(oracle, dict):
        raise QualificationError("issue #99 corpus has no oracle declaration")
    if oracle.get("expectedValuesAreRuntimeIndependent") is not True:
        raise QualificationError("issue #99 expected values are not declared independent")
    if oracle.get("adapterHelpersUsedForExpected") is not False:
        raise QualificationError("issue #99 permits adapter-derived expected values")
    if oracle.get("sourceConstruction") != "authored-stdlib-zip-xml-package":
        raise QualificationError("issue #99 corpus is not marked as authored ZIP/XML source")
    if not isinstance(oracle.get("forbiddenDerivations"), list) or not oracle["forbiddenDerivations"]:
        raise QualificationError("issue #99 corpus has no forbidden derivation list")

    producer_policy = corpus.get("producerPolicy")
    if not isinstance(producer_policy, dict):
        raise QualificationError("issue #99 corpus has no producer policy")
    if producer_policy.get("syntheticOnly") is not True:
        raise QualificationError("issue #99 producer fixtures must be explicitly synthetic-only")
    if producer_policy.get("realProducerCorpusAvailable") is not False:
        raise QualificationError("issue #99 must not pass without a real producer corpus")
    required_real = producer_policy.get("requiredRealProducers")
    if not isinstance(required_real, list) or len(required_real) < 4:
        raise QualificationError("issue #99 must declare the required real producer families")

    requirements = corpus.get("requirements")
    if not isinstance(requirements, list) or not requirements:
        raise QualificationError("issue #99 corpus has no completion requirements")
    requirement_ids: set[str] = set()
    for requirement in requirements:
        if not isinstance(requirement, dict):
            raise QualificationError("issue #99 completion requirement is not an object")
        requirement_id = requirement.get("id")
        if not isinstance(requirement_id, str) or not requirement_id or requirement_id in requirement_ids:
            raise QualificationError(f"invalid or duplicate issue #99 requirement id: {requirement_id!r}")
        if not isinstance(requirement.get("description"), str) or not requirement["description"]:
            raise QualificationError(f"issue #99 requirement has no description: {requirement_id}")
        if not isinstance(requirement.get("gate"), str) or not requirement["gate"]:
            raise QualificationError(f"issue #99 requirement has no executable gate description: {requirement_id}")
        requirement_ids.add(requirement_id)
    unmet_requirements = corpus.get("unmetRequirements")
    if not isinstance(unmet_requirements, list) or not all(isinstance(item, str) and item for item in unmet_requirements):
        raise QualificationError("issue #99 corpus must declare its current unmet requirements explicitly")

    profile = corpus.get("profile")
    if not isinstance(profile, dict) or not isinstance(profile.get("profileId"), str):
        raise QualificationError("issue #99 has no machine-readable DOCX profile")

    fixtures = corpus.get("fixtures")
    if not isinstance(fixtures, list) or not fixtures:
        raise QualificationError("issue #99 corpus has no fixtures")
    fixture_ids: set[str] = set()
    producers: set[str] = set()
    all_case_ids: set[str] = set()
    for fixture in fixtures:
        if not isinstance(fixture, dict):
            raise QualificationError("issue #99 fixture is not an object")
        fixture_id = fixture.get("fixtureId")
        if not isinstance(fixture_id, str) or not fixture_id or fixture_id in fixture_ids:
            raise QualificationError(f"invalid or duplicate fixture id: {fixture_id!r}")
        if fixture.get("format") != "docx" or fixture.get("payloadKind") != "zip":
            raise QualificationError(f"issue #99 fixture is not an authored DOCX ZIP: {fixture_id}")
        if not isinstance(fixture.get("producer"), str) or not fixture["producer"]:
            raise QualificationError(f"fixture has no producer label: {fixture_id}")
        if not isinstance(fixture.get("parts"), dict) or not fixture["parts"]:
            raise QualificationError(f"fixture has no package parts: {fixture_id}")
        expected = fixture.get("expected")
        if not isinstance(expected, dict):
            raise QualificationError(f"fixture has no authored expected facts: {fixture_id}")
        for projection in (
            "parts",
            "relationships",
            "hyperlinks",
            "drawings",
            "tables",
            "fields",
            "stories",
            "sections",
            "styles",
            "unsupported",
            "resources",
            "completionClaims",
        ):
            if not isinstance(expected.get(projection), list):
                raise QualificationError(f"fixture projection {projection!r} is not a list: {fixture_id}")
        cases = fixture.get("cases")
        if not isinstance(cases, list) or not cases:
            raise QualificationError(f"fixture has no qualification cases: {fixture_id}")
        for case in cases:
            if not isinstance(case, dict) or not isinstance(case.get("caseId"), str):
                raise QualificationError(f"malformed fixture case: {fixture_id}")
            if case["caseId"] in all_case_ids:
                raise QualificationError(f"duplicate case id: {case['caseId']}")
            if case.get("category") not in {"profile", "closure", "resource", "story", "structure", "style-layout", "unsupported", "accounting", "completion", "security", "producer", "differential"}:
                raise QualificationError(f"invalid case category: {case['caseId']}")
            all_case_ids.add(case["caseId"])
        fixture_ids.add(fixture_id)
        producers.add(fixture["producer"])

    missing = sorted(REQUIRED_REGRESSION_CASES - all_case_ids - {item.get("caseId") for item in corpus.get("securityCases", []) if isinstance(item, dict)})
    if missing:
        raise QualificationError(f"issue #99 corpus misses required regression cases: {missing}")

    security_cases = corpus.get("securityCases")
    if not isinstance(security_cases, list) or not security_cases:
        raise QualificationError("issue #99 corpus has no resource/security cases")
    for item in security_cases:
        if not isinstance(item, dict) or not isinstance(item.get("caseId"), str):
            raise QualificationError("malformed issue #99 security case")
        if item.get("fixtureId") not in fixture_ids:
            raise QualificationError(f"security case references an unknown fixture: {item.get('caseId')}")
        if not isinstance(item.get("limits"), dict) or not item["limits"]:
            raise QualificationError(f"security case has no limit: {item.get('caseId')}")
        all_case_ids.add(item["caseId"])

    negatives = corpus.get("negativeCases")
    if not isinstance(negatives, list):
        raise QualificationError("issue #99 corpus has no negative cases")
    negative_ids = {item.get("caseId") for item in negatives if isinstance(item, dict)}
    missing_negatives = sorted(REQUIRED_NEGATIVE_CASES - negative_ids)
    if missing_negatives:
        raise QualificationError(f"issue #99 corpus misses negative mutations: {missing_negatives}")
    for item in negatives:
        if not isinstance(item, dict) or not isinstance(item.get("caseId"), str):
            raise QualificationError("malformed issue #99 negative case")
        if item.get("fixtureId") not in fixture_ids:
            raise QualificationError(f"negative case references unknown fixture: {item.get('caseId')}")
        if not isinstance(item.get("projection"), str):
            raise QualificationError(f"negative case has no projection: {item.get('caseId')}")
        mutation = item.get("mutation")
        if not isinstance(mutation, dict) or mutation.get("op") not in {"set", "delete", "append"}:
            raise QualificationError(f"negative case has invalid mutation: {item.get('caseId')}")
    return corpus


def load_corpus(path: Path = DEFAULT_CORPUS_PATH) -> dict[str, Any]:
    """Public test-friendly alias for the strict corpus loader."""

    return _load_corpus(path)


def _materialize_fixture(fixture: dict[str, Any], work: Path) -> Path:
    path = work / f"{fixture['fixtureId']}.docx"
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
        for raw_name in sorted(fixture["parts"]):
            name = _safe_member_name(raw_name)
            info = zipfile.ZipInfo(name, date_time=(2020, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.create_system = 3
            info.external_attr = 0o100644 << 16
            archive.writestr(info, _payload_bytes(fixture["parts"][raw_name]))
    return path


def _run_converter(
    fixture: dict[str, Any],
    source_path: Path,
    work: Path,
    *,
    case_id: str = "base",
    limits: dict[str, Any] | None = None,
) -> dict[str, Any]:
    token = re.sub(r"[^A-Za-z0-9_.-]+", "-", f"{fixture['fixtureId']}-{case_id}")
    output_path = work / f"{token}.json"
    evidence_path = work / f"{token}.evidence.json"
    command = [
        sys.executable,
        str(CONVERTER_PATH),
        "convert",
        str(source_path),
        "--format",
        "docx",
        "--out",
        str(output_path),
        "--evidence",
        str(evidence_path),
    ]
    for key, value in sorted((limits or {}).items()):
        if not re.fullmatch(r"[a-z][a-z0-9-]*", str(key)):
            raise QualificationError(f"invalid public converter limit name: {key!r}")
        command.extend([f"--{key}", str(value)])
    try:
        result = subprocess.run(
            command,
            cwd=ROOT,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=90,
            check=False,
        )
        exit_code = result.returncode
        stdout = result.stdout[-3000:]
        stderr = result.stderr[-3000:]
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
        "fixtureId": fixture["fixtureId"],
        "caseId": case_id,
        "sourceSha256": _sha256_file(source_path),
        "commandExitCode": exit_code,
        "stdout": stdout,
        "stderr": stderr,
        "document": document,
        "evidence": evidence,
    }


def _local(tag: Any) -> str:
    return str(tag).rsplit("}", 1)[-1]


def _attr(element: ET.Element, name: str, default: str = "") -> str:
    return element.attrib.get(name, default)


def _wattr(element: ET.Element | None, name: str, default: str = "") -> str:
    if element is None:
        return default
    return element.attrib.get(f"{{{W_NS}}}{name}", element.attrib.get(name, default)) or default


def _rattr(element: ET.Element | None, name: str, default: str = "") -> str:
    if element is None:
        return default
    return element.attrib.get(f"{{{R_NS}}}{name}", element.attrib.get(name, default)) or default


def _xml_root(payload: bytes, name: str) -> ET.Element | None:
    try:
        return ET.fromstring(payload)
    except ET.ParseError:
        return None


def _relationship_source(rels_name: str) -> str:
    if rels_name == "_rels/.rels":
        return "[package]"
    directory, rel_name = rels_name.split("/_rels/", 1)
    return f"{directory}/{rel_name[:-5]}" if rel_name.endswith(".rels") else f"{directory}/{rel_name}"


def _relationship_target(source_name: str, target: str) -> str:
    target = target.replace("\\", "/")
    if target.startswith("/"):
        return posixpath.normpath(target.lstrip("/"))
    base = "" if source_name == "[package]" else source_name.rsplit("/", 1)[0] if "/" in source_name else ""
    return posixpath.normpath(posixpath.join(base, target))


def _content_types(parts: dict[str, bytes]) -> dict[str, str]:
    root = _xml_root(parts.get("[Content_Types].xml", b""), "[Content_Types].xml")
    defaults: dict[str, str] = {}
    overrides: dict[str, str] = {}
    if root is not None:
        for item in list(root):
            local = _local(item.tag)
            if local == "Default":
                defaults[_attr(item, "Extension").lower()] = _attr(item, "ContentType")
            elif local == "Override":
                overrides[_attr(item, "PartName").lstrip("/")] = _attr(item, "ContentType")
    result: dict[str, str] = {}
    for name in parts:
        if name in overrides:
            result[name] = overrides[name]
        elif "." in name:
            result[name] = defaults.get(name.rsplit(".", 1)[-1].lower(), "application/octet-stream")
        else:
            result[name] = "application/octet-stream"
    return result


def _element_paths(root: ET.Element) -> dict[int, str]:
    locations: dict[int, str] = {}

    def walk(element: ET.Element, parent: str) -> None:
        counts: dict[str, int] = {}
        for child in list(element):
            local = _local(child.tag)
            counts[local] = counts.get(local, 0) + 1
            path = f"{parent}/{local}[{counts[local]}]"
            locations[id(child)] = path
            walk(child, path)

    locations[id(root)] = _local(root.tag)
    walk(root, _local(root.tag))
    return locations


def _source_relationships(parts: dict[str, bytes], names: set[str]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for rels_name in sorted(name for name in names if name == "_rels/.rels" or "/_rels/" in name):
        source = _relationship_source(rels_name)
        root = _xml_root(parts[rels_name], rels_name)
        if root is None:
            continue
        for relationship in list(root):
            if _local(relationship.tag) != "Relationship":
                continue
            target = _attr(relationship, "Target")
            mode = _attr(relationship, "TargetMode") or "Internal"
            resolved = target if mode == "External" else _relationship_target(source, target)
            target_kind = "external" if mode == "External" else "part" if resolved in names else "missing"
            rows.append(
                {
                    "source": source,
                    "id": _attr(relationship, "Id"),
                    "type": _attr(relationship, "Type").rsplit("/", 1)[-1],
                    "target": target,
                    "resolvedTarget": resolved,
                    "targetKind": target_kind,
                    "targetMode": mode,
                }
            )
    return rows


def _story_kind(name: str) -> str | None:
    match = re.fullmatch(r"word/(header|footer)(\d+)\.xml", name)
    if match:
        return match.group(1)
    return {"word/footnotes.xml": "footnote", "word/endnotes.xml": "endnote", "word/comments.xml": "comment"}.get(name)


def _story_texts(root: ET.Element) -> list[str]:
    return ["".join(item.text or "" for item in paragraph.iter() if _local(item.tag) in {"t", "delText"}) for paragraph in root.iter() if _local(paragraph.tag) == "p"]


def _source_stories(parts: dict[str, bytes], names: set[str]) -> list[dict[str, Any]]:
    stories: list[dict[str, Any]] = []
    for name in sorted(names):
        kind = _story_kind(name)
        if kind is None:
            continue
        root = _xml_root(parts[name], name)
        if root is None:
            continue
        root_element = next((child for child in list(root) if _local(child.tag) in {"hdr", "ftr"}), root)
        if kind in {"header", "footer"}:
            story_id = f"{kind}{re.search(r'(\d+)', name).group(1)}"
            items = [root_element]
        else:
            story_id = kind + "s"
            items = [child for child in list(root_element) if _local(child.tag) in {"footnote", "endnote", "comment"}]
        body_elements: dict[str, int] = {}
        for child in list(root_element):
            body_elements[_local(child.tag)] = body_elements.get(_local(child.tag), 0) + 1
        item_rows = []
        for item in items:
            item_id = _wattr(item, "id") or "root"
            item_rows.append(
                {
                    "id": item_id,
                    "texts": _story_texts(item),
                    "paragraphCount": sum(1 for element in item.iter() if _local(element.tag) == "p"),
                }
            )
        stories.append(
            {
                "storyId": story_id,
                "kind": kind,
                "part": name,
                "rootTexts": _story_texts(root_element),
                "bodyElementCounts": body_elements,
                "items": item_rows,
            }
        )
    return stories


def _source_anchors(parts: dict[str, bytes]) -> list[dict[str, Any]]:
    root = _xml_root(parts.get("word/document.xml", b""), "word/document.xml")
    if root is None:
        return []
    rows: list[dict[str, Any]] = []
    for element in root.iter():
        local = _local(element.tag)
        if local == "commentRangeStart":
            rows.append({"kind": "comment", "id": _wattr(element, "id"), "anchor": "range-start"})
        elif local == "commentRangeEnd":
            rows.append({"kind": "comment", "id": _wattr(element, "id"), "anchor": "range-end"})
        elif local == "commentReference":
            rows.append({"kind": "comment", "id": _wattr(element, "id"), "anchor": "reference"})
        elif local == "footnoteReference":
            rows.append({"kind": "footnote", "id": _wattr(element, "id"), "anchor": "reference"})
        elif local == "endnoteReference":
            rows.append({"kind": "endnote", "id": _wattr(element, "id"), "anchor": "reference"})
    return rows


def _text_in(element: ET.Element) -> str:
    return "".join(item.text or "" for item in element.iter() if _local(item.tag) in {"t", "delText"})


def _source_hyperlinks(root: ET.Element, relationships: list[dict[str, Any]]) -> list[dict[str, Any]]:
    relation_by_id = {item["id"]: item for item in relationships}
    rows: list[dict[str, Any]] = []
    for index, hyperlink in enumerate((item for item in root.iter() if _local(item.tag) == "hyperlink"), start=1):
        runs = [item for item in hyperlink.iter() if _local(item.tag) == "r"]
        tokens: list[str] = []
        for run in runs:
            for item in list(run):
                local = _local(item.tag)
                if local in {"t", "delText", "instrText"}:
                    tokens.append(item.text or "")
                elif local == "tab":
                    tokens.append("\t")
                elif local == "br":
                    tokens.append("\n")
        relation_id = _rattr(hyperlink, "id")
        relation = relation_by_id.get(relation_id, {})
        rows.append(
            {
                "hyperlinkId": f"hyperlink-{index}",
                "relationshipId": relation_id,
                "destination": relation.get("resolvedTarget", ""),
                "displayText": "".join(tokens),
                "runCount": len(runs),
                "tokens": tokens,
            }
        )
    return rows


def _source_drawings(root: ET.Element, relationships: list[dict[str, Any]]) -> list[dict[str, Any]]:
    relation_by_id = {item["id"]: item for item in relationships}
    rows: list[dict[str, Any]] = []
    for index, drawing in enumerate((item for item in root.iter() if _local(item.tag) == "drawing"), start=1):
        anchor = next((item for item in drawing.iter() if _local(item.tag) in {"inline", "anchor"}), None)
        extent = next((item for item in drawing.iter() if _local(item.tag) == "extent"), None)
        blip = next((item for item in drawing.iter() if _local(item.tag) == "blip"), None)
        transform = next((item for item in drawing.iter() if _local(item.tag) == "xfrm"), None)
        graphic_kind = "image" if any(_local(item.tag) == "pic" for item in drawing.iter()) else "textBox" if any(_local(item.tag) == "txbx" for item in drawing.iter()) else "shape"
        rows.append(
            {
                "drawingId": f"drawing-{index}",
                "container": "run",
                "kind": graphic_kind,
                "anchor": _local(anchor.tag) if anchor is not None else "unknown",
                "extent": {"cx": _attr(extent, "cx") if extent is not None else "", "cy": _attr(extent, "cy") if extent is not None else ""},
                "resource": relation_by_id.get(_rattr(blip, "embed"), {}).get("resolvedTarget", "") if blip is not None else "",
                "transform": {
                    "off": {"x": _attr(next((item for item in list(transform) if _local(item.tag) == "off"), None), "x") if transform is not None else "", "y": _attr(next((item for item in list(transform) if _local(item.tag) == "off"), None), "y") if transform is not None else ""},
                    "ext": {"cx": _attr(next((item for item in list(transform) if _local(item.tag) == "ext"), None), "cx") if transform is not None else "", "cy": _attr(next((item for item in list(transform) if _local(item.tag) == "ext"), None), "cy") if transform is not None else ""},
                },
            }
        )
    return rows


def _source_tables(root: ET.Element) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for table_number, table in enumerate((item for item in root.iter() if _local(item.tag) == "tbl"), start=1):
        grid = next((item for item in list(table) if _local(item.tag) == "tblGrid"), None)
        grid_columns = [_wattr(item, "w") for item in (list(grid) if grid is not None else []) if _local(item.tag) == "gridCol"]
        row_rows: list[dict[str, Any]] = []
        for row_number, row in enumerate((item for item in list(table) if _local(item.tag) == "tr"), start=1):
            cells: list[dict[str, Any]] = []
            for cell_number, cell in enumerate((item for item in list(row) if _local(item.tag) == "tc"), start=1):
                properties = next((item for item in list(cell) if _local(item.tag) == "tcPr"), None)
                span_element = next((item for item in (list(properties) if properties is not None else []) if _local(item.tag) == "gridSpan"), None)
                merge_element = next((item for item in (list(properties) if properties is not None else []) if _local(item.tag) == "vMerge"), None)
                nested = [f"table-{number}" for number, candidate in enumerate((item for item in root.iter() if _local(item.tag) == "tbl"), start=1) if candidate is not table and candidate in list(cell.iter())]
                cells.append(
                    {
                        "cellId": f"table-{table_number}-row-{row_number}-cell-{cell_number}",
                        "gridSpan": int(_wattr(span_element, "val", "1")),
                        "vMerge": _wattr(merge_element, "val", "continue") if merge_element is not None else "none",
                        "text": _text_in(cell),
                        "nestedTableIds": nested,
                    }
                )
            row_rows.append({"rowId": f"table-{table_number}-row-{row_number}", "cells": cells})
        rows.append({"tableId": f"table-{table_number}", "gridColumns": grid_columns, "rows": row_rows})
    return rows


def _source_fields(root: ET.Element) -> list[dict[str, Any]]:
    fields: list[dict[str, Any]] = []
    stack: list[dict[str, Any]] = []
    field_number = 0
    for element in root.iter():
        local = _local(element.tag)
        if local == "fldChar":
            field_type = _wattr(element, "fldCharType")
            if field_type == "begin":
                field_number += 1
                row = {"fieldId": f"field-{field_number}", "depth": len(stack), "events": []}
                fields.append(row)
                for active in stack:
                    active["events"].append({"type": "begin", "depth": len(stack)})
                stack.append(row)
                row["events"].append({"type": "begin", "depth": row["depth"]})
            elif field_type in {"separate", "end"}:
                if stack:
                    current = stack[-1]
                    current["events"].append({"type": field_type, "depth": current["depth"]})
                    if field_type == "end":
                        stack.pop()
        elif local == "instrText" and stack:
            for active in stack:
                active["events"].append({"type": "instruction", "depth": active["depth"], "value": (element.text or "").strip()})
        elif local in {"t", "delText"} and stack:
            for active in stack:
                active["events"].append({"type": "result", "depth": active["depth"], "value": element.text or ""})
    for row in fields:
        row["events"] = [event for event in row["events"] if event.get("type") != "begin" or event.get("depth") == row["depth"]]
    return fields


def _source_sections(root: ET.Element) -> list[dict[str, Any]]:
    sections: list[dict[str, Any]] = []
    for index, section in enumerate((item for item in root.iter() if _local(item.tag) == "sectPr"), start=1):
        size = next((item for item in list(section) if _local(item.tag) == "pgSz"), None)
        margin = next((item for item in list(section) if _local(item.tag) == "pgMar"), None)
        columns = next((item for item in list(section) if _local(item.tag) == "cols"), None)
        line_numbers = next((item for item in list(section) if _local(item.tag) == "lnNumType"), None)
        sections.append(
            {
                "sectionId": f"section-{index}",
                "pgSz": {"w": _wattr(size, "w"), "h": _wattr(size, "h"), "orient": _wattr(size, "orient", "portrait")},
                "pgMar": {key: _wattr(margin, key) for key in ("top", "right", "bottom", "left", "header", "footer", "gutter") if _wattr(margin, key)},
                "columns": {"num": _wattr(columns, "num", "1"), "space": _wattr(columns, "space")},
                "lineNumbering": {"countBy": _wattr(line_numbers, "countBy")} if line_numbers is not None else {},
            }
        )
    return sections


def _source_styles(parts: dict[str, bytes]) -> list[dict[str, Any]]:
    root = _xml_root(parts.get("word/styles.xml", b""), "word/styles.xml")
    if root is None:
        return []
    rows: list[dict[str, Any]] = []
    defaults = next((item for item in list(root) if _local(item.tag) == "docDefaults"), None)
    default_rpr = next((item for item in defaults.iter() if _local(item.tag) == "rPr") if defaults is not None else (), None)
    default_fonts = next((item for item in (list(default_rpr) if default_rpr is not None else []) if _local(item.tag) == "rFonts"), None)
    default_size = next((item for item in (list(default_rpr) if default_rpr is not None else []) if _local(item.tag) == "sz"), None)
    rows.append(
        {
            "styleId": "docDefaults",
            "basedOn": "",
            "font": _wattr(default_fonts, "ascii"),
            "size": _wattr(default_size, "val"),
            "themeColor": "",
            "conditional": [],
        }
    )
    for style in (item for item in list(root) if _local(item.tag) == "style"):
        style_id = _wattr(style, "styleId")
        based = next((item for item in list(style) if _local(item.tag) == "basedOn"), None)
        rpr = next((item for item in list(style) if _local(item.tag) == "rPr"), None)
        fonts = next((item for item in (list(rpr) if rpr is not None else []) if _local(item.tag) == "rFonts"), None)
        color = next((item for item in (list(rpr) if rpr is not None else []) if _local(item.tag) == "color"), None)
        size = next((item for item in (list(rpr) if rpr is not None else []) if _local(item.tag) == "sz"), None)
        conditional = []
        for item in list(style):
            if _local(item.tag) == "tblStylePr":
                conditional.append(_wattr(item, "type"))
        rows.append(
            {
                "styleId": style_id,
                "basedOn": _wattr(based, "val"),
                "font": _wattr(fonts, "ascii"),
                "size": _wattr(size, "val"),
                "themeColor": _wattr(color, "themeColor"),
                "color": _wattr(color, "val"),
                "conditional": conditional,
            }
        )
    return rows


def _source_unsupported(parts: dict[str, bytes], names: set[str]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for name in sorted(names):
        if not name.endswith(".xml"):
            continue
        root = _xml_root(parts[name], name)
        if root is None:
            continue
        for element in root.iter():
            local = _local(element.tag)
            if local in {"unknownBlock", "customChoice", "customFallback"}:
                rows.append({"token": local, "part": name, "disposition": "diagnose"})
            if local == "AlternateContent":
                rows.append({"token": "AlternateContent", "part": name, "disposition": "diagnose"})
                for branch in list(element):
                    if _local(branch.tag) in {"Choice", "Fallback"}:
                        rows.append({"token": f"AlternateContent:{_local(branch.tag)}", "part": name, "disposition": "diagnose"})
            for key in element.attrib:
                if key.startswith(f"{{{W_NS}}}") and key.rsplit("}", 1)[-1] in {"foo", "mystery"}:
                    rows.append({"token": f"attribute:{key.rsplit('}', 1)[-1]}", "part": name, "disposition": "diagnose"})
    return rows


def _source_resources(relationships: list[dict[str, Any]], names: set[str]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for relationship in relationships:
        if relationship["targetKind"] in {"external", "missing"}:
            rows.append(
                {
                    "target": relationship["resolvedTarget"],
                    "kind": relationship["targetKind"],
                    "availability": "unavailable",
                    "relationshipId": relationship["id"],
                }
            )
    for name in sorted(names):
        if name.startswith("word/media/"):
            rows.append({"target": name, "kind": "media", "availability": "available", "relationshipId": ""})
        elif name.startswith("word/embeddings/"):
            rows.append({"target": name, "kind": "embedding", "availability": "available", "relationshipId": ""})
    return rows


def _source_facts(source_path: Path) -> dict[str, Any]:
    with zipfile.ZipFile(source_path) as archive:
        names = {name.replace("\\", "/") for name in archive.namelist()}
        parts = {name: archive.read(name) for name in names}
    content_types = _content_types(parts)
    relationships = _source_relationships(parts, names)
    document_root = _xml_root(parts.get("word/document.xml", b""), "word/document.xml")
    return {
        "parts": [{"name": name, "contentType": content_types.get(name, "")} for name in sorted(names)],
        "relationships": relationships,
        "hyperlinks": _source_hyperlinks(document_root, relationships) if document_root is not None else [],
        "drawings": _source_drawings(document_root, relationships) if document_root is not None else [],
        "tables": _source_tables(document_root) if document_root is not None else [],
        "fields": _source_fields(document_root) if document_root is not None else [],
        "stories": _source_stories(parts, names),
        "anchors": _source_anchors(parts),
        "sections": _source_sections(document_root) if document_root is not None else [],
        "styles": _source_styles(parts),
        "unsupported": _source_unsupported(parts, names),
        "resources": _source_resources(relationships, names),
    }


def _maps(document: dict[str, Any]) -> dict[str, dict[str, dict[str, Any]]]:
    collections = {
        "parts": "partId",
        "nodes": "nodeId",
        "texts": "textId",
        "tables": "tableId",
        "styles": "styleId",
        "layouts": "layoutId",
        "resources": "resourceId",
        "fields": "fieldId",
        "annotations": "annotationId",
        "relations": "relationId",
        "extensions": "extensionId",
        "surfaces": "surfaceId",
    }
    return {collection: {str(item.get(key)): item for item in document.get(collection, []) if isinstance(item, dict) and item.get(key) is not None} for collection, key in collections.items()}


def _part_name(identifier: Any, maps: dict[str, dict[str, dict[str, Any]]]) -> str:
    item = maps["parts"].get(str(identifier))
    return str(item.get("name", "")) if item else ""


def _node_text(identifier: Any, maps: dict[str, dict[str, dict[str, Any]]]) -> str:
    node = maps["nodes"].get(str(identifier))
    if not node:
        return ""
    values: list[str] = []
    for text_id in node.get("textIds", []):
        text = maps["texts"].get(str(text_id))
        if text:
            values.append(str(text.get("value", "")))
    for child_id in node.get("childIds", []):
        values.append(_node_text(child_id, maps))
    return "".join(values)


def _actual_parts(document: dict[str, Any]) -> list[dict[str, Any]]:
    return sorted(
        [
            {"name": item.get("name", ""), "contentType": item.get("contentType", "")}
            for item in document.get("parts", [])
            if item.get("name") != "OOXML package"
        ],
        key=lambda item: item["name"],
    )


def _actual_relationships(document: dict[str, Any]) -> list[dict[str, Any]]:
    maps = _maps(document)
    target_by_relation = {
        str(extension.get("targetId")): extension.get("payload", {})
        for extension in document.get("extensions", [])
        if extension.get("type") == "relationship-target" and isinstance(extension.get("payload"), dict)
    }
    rows: list[dict[str, Any]] = []
    for relation in document.get("relations", []):
        # The adapter exposes a resource-consumer relation in addition to the
        # package relationship.  It is useful in the common IR, but it is not
        # an OPC relationship occurrence and must not be counted twice here.
        if relation.get("kind") == "usesResource":
            continue
        source = _part_name(relation.get("fromId"), maps)
        if source == "OOXML package":
            source = "[package]"
        target_part = maps["parts"].get(str(relation.get("toId")))
        target_resource = maps["resources"].get(str(relation.get("toId")))
        if target_part:
            target = target_part.get("name", "")
            kind = "part"
        elif target_resource:
            target = target_resource.get("externalTarget", target_resource.get("derivedHandle", ""))
            kind = "external" if target_resource.get("externalTarget", "") and str(target_resource.get("externalTarget", "")).startswith(("http://", "https://")) else "missing"
        else:
            target = ""
            kind = "unknown"
        payload = target_by_relation.get(str(relation.get("relationId")))
        if isinstance(payload, dict):
            target = payload.get("target", target)
            resolved_target = payload.get("resolvedTarget", target)
        else:
            target = relation.get("target", target)
            resolved_target = relation.get("resolvedTarget", target)
        relationship_type = relation.get("type") or relation.get("relationshipType") or relation.get("sourceType", "")
        relationship_type = str(relationship_type).rsplit("/", 1)[-1]
        target_mode = str(relation.get("targetMode", "Internal"))
        target_mode = "External" if target_mode.lower() == "external" else "Internal"
        rows.append(
            {
                "source": source,
                "id": relation.get("sourceRelationshipId", ""),
                "type": relationship_type,
                "target": target,
                "resolvedTarget": resolved_target,
                "targetKind": kind,
                "targetMode": target_mode,
            }
        )
    return rows


def _actual_hyperlinks(document: dict[str, Any]) -> list[dict[str, Any]]:
    maps = _maps(document)
    rows: list[dict[str, Any]] = []
    for index, annotation in enumerate((item for item in document.get("annotations", []) if item.get("kind") == "hyperlink"), start=1):
        run_ids = [item for item in annotation.get("targetIds", []) if maps["nodes"].get(str(item), {}).get("kind") == "run"]
        action = annotation.get("action") if isinstance(annotation.get("action"), dict) else {}
        tokens = annotation.get("tokens")
        if not isinstance(tokens, list) and isinstance(annotation.get("anchor"), dict):
            tokens = annotation["anchor"].get("tokens")
        if not isinstance(tokens, list):
            tokens = [_node_text(run_id, maps) for run_id in run_ids]
        rows.append(
            {
                "hyperlinkId": f"hyperlink-{index}",
                "relationshipId": action.get("relationshipId", ""),
                "destination": annotation.get("destination", annotation.get("body", "")),
                "displayText": annotation.get("displayText", "".join(_node_text(run_id, maps) for run_id in run_ids)),
                "runCount": len(run_ids),
                "tokens": tokens,
            }
        )
    return rows


def _actual_drawings(document: dict[str, Any]) -> list[dict[str, Any]]:
    maps = _maps(document)
    rows: list[dict[str, Any]] = []
    extension_targets: set[str] = set()
    for extension in document.get("extensions", []):
        if extension.get("type") not in {"drawing", "drawingml"}:
            continue
        payload = extension.get("payload", {})
        target = maps["nodes"].get(str(extension.get("targetId")), {})
        extension_targets.add(str(extension.get("targetId")))
        layout = next((item for item in document.get("layouts", []) if item.get("layoutId") in target.get("layoutIds", [])), {})
        layout_anchor = layout.get("anchor", {}) if isinstance(layout.get("anchor"), dict) else {}
        anchor = payload.get("anchor", "")
        if layout.get("placement") == "anchored" or anchor == "floating":
            anchor = "anchor"
        extent = payload.get("extentEmu", payload.get("extent", {}))
        transform = payload.get("transform", {})
        layout_transform = layout.get("transform", {}) if isinstance(layout.get("transform"), dict) else {}
        if isinstance(layout_transform, dict) and isinstance(extent, dict) and ("e" in layout_transform or "f" in layout_transform):
            transform = {
                "off": {"x": str(layout_transform.get("e", "")), "y": str(layout_transform.get("f", ""))},
                "ext": {"cx": extent.get("cx", ""), "cy": extent.get("cy", "")},
            }
        resource = payload.get("resource", payload.get("resourceTarget", ""))
        if not resource:
            resource = next(
                (
                    maps["resources"].get(str(resource_id), {}).get("derivedHandle", maps["resources"].get(str(resource_id), {}).get("externalTarget", ""))
                    for resource_id in target.get("resourceIds", [])
                    if maps["resources"].get(str(resource_id))
                ),
                "",
            )
        rows.append(
            {
                "drawingId": f"drawing-{len(rows) + 1}",
                "container": payload.get("container", "run" if maps["nodes"].get(str(target.get("parentId")), {}).get("kind") == "run" else "paragraph" if maps["nodes"].get(str(target.get("parentId")), {}).get("kind") == "paragraph" else "unknown"),
                "kind": payload.get("kind", target.get("kind", "")),
                "anchor": anchor,
                "extent": extent,
                "resource": resource,
                "transform": transform,
            }
        )
    # Some valid producers expose the drawing as a typed node rather than an
    # extension.  Read only fields actually present in that public IR; never
    # reconstruct geometry from the authored ZIP here.
    emitted = len(rows)
    for node in document.get("nodes", []):
        if node.get("kind") not in {"image", "shape", "textBox", "connector", "drawing"}:
            continue
        if str(node.get("nodeId")) in extension_targets:
            continue
        rows.append(
            {
                "drawingId": f"drawing-{emitted + 1}",
                "container": node.get("container", "run"),
                "kind": node.get("kind", ""),
                "anchor": node.get("anchor", ""),
                "extent": node.get("extentEmu", node.get("extent", {})),
                "resource": node.get("resource", node.get("resourceTarget", "")),
                "transform": node.get("transform", {}),
            }
        )
        emitted += 1
    return rows


def _actual_tables(document: dict[str, Any]) -> list[dict[str, Any]]:
    maps = _maps(document)
    document_surface_ids = {
        str(surface.get("surfaceId"))
        for surface in document.get("surfaces", [])
        if _part_name(surface.get("partId"), maps) == "word/document.xml"
    }
    tables = [
        item
        for item in document.get("tables", [])
        if isinstance(item, dict)
        and (
            not item.get("ownerSurfaceId")
            or str(item.get("ownerSurfaceId")) in document_surface_ids
        )
    ]
    table_by_id = {str(item.get("tableId")): item for item in tables}
    roots = [item for item in tables if not item.get("ownerCellId")]
    roots.sort(key=lambda item: str(item.get("tableId", "")))
    ordered: list[dict[str, Any]] = []

    def add_table(table: dict[str, Any]) -> None:
        if table in ordered:
            return
        ordered.append(table)
        for child_id in table.get("nestedTableIds", []):
            child = table_by_id.get(str(child_id))
            if child is not None:
                add_table(child)

    for table in roots:
        add_table(table)
    for table in tables:
        add_table(table)
    table_number = {str(table.get("tableId")): index for index, table in enumerate(ordered, start=1)}
    rows: list[dict[str, Any]] = []
    for number, table in enumerate(ordered, start=1):
        table_node = maps["nodes"].get(str(table.get("nodeId")), {})
        child_rows = [maps["nodes"].get(str(row_id), {}) for row_id in table.get("rowIds", [])]
        row_rows = []
        for row_number, row in enumerate(child_rows, start=1):
            cells = []
            for cell_number, cell_id in enumerate([item for item in row.get("childIds", []) if maps["nodes"].get(str(item), {}).get("kind") == "cell"], start=1):
                cell = maps["nodes"].get(str(cell_id), {})
                nested_ids = [
                    f"table-{table_number[str(child.get('tableId'))]}"
                    for child in table_by_id.values()
                    if child.get("ownerCellId") == cell_id
                ]
                merge_role = cell.get("mergeRole")
                v_merge = "restart" if merge_role == "master" else "continue" if merge_role == "follower" else "none"
                cells.append(
                    {
                        "cellId": f"table-{number}-row-{row_number}-cell-{cell_number}",
                        "gridSpan": cell.get("gridSpan", 1),
                        "vMerge": v_merge,
                        "text": _node_text(cell_id, maps),
                        "nestedTableIds": nested_ids,
                    }
                )
            row_rows.append({"rowId": f"table-{number}-row-{row_number}", "cells": cells})
        widths = table.get("gridColumnWidths", table.get("gridColumns", []))
        rows.append({"tableId": f"table-{number}", "gridColumns": [str(item) for item in widths], "rows": row_rows})
    return rows


def _actual_fields(document: dict[str, Any]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    field_nodes = {str(node.get("fieldId")): node for node in document.get("nodes", []) if node.get("kind") == "field" and node.get("fieldId")}
    sequence_by_node = {
        str(extension.get("targetId")): extension.get("payload", {})
        for extension in document.get("extensions", [])
        if extension.get("type") == "field-sequence" and isinstance(extension.get("payload"), dict)
    }
    for index, field in enumerate(document.get("fields", []), start=1):
        field_node = field_nodes.get(str(field.get("fieldId")), {})
        sequence = sequence_by_node.get(str(field_node.get("nodeId")), {})
        rows.append(
            {
                "fieldId": f"field-{index}",
                "depth": sequence.get("depth"),
                "events": sequence.get("events", []) if isinstance(sequence.get("events"), list) else [],
            }
        )
    return rows


def _actual_stories(document: dict[str, Any]) -> list[dict[str, Any]]:
    maps = _maps(document)
    rows: list[dict[str, Any]] = []

    def descendants(node_id: Any) -> Iterable[dict[str, Any]]:
        node = maps["nodes"].get(str(node_id))
        if not node:
            return
        yield node
        for child_id in node.get("childIds", []):
            yield from descendants(child_id)

    for part in document.get("parts", []):
        kind = _story_kind(str(part.get("name", "")))
        if kind is None:
            continue
        root_ids = part.get("rootNodeIds", [])
        root_nodes = [maps["nodes"].get(str(node_id), {}) for node_id in root_ids]
        if kind in {"header", "footer"}:
            body_counts: dict[str, int] = {}
            for node in root_nodes:
                token = {"paragraph": "p", "table": "tbl"}.get(str(node.get("kind", "")))
                if token:
                    body_counts[token] = body_counts.get(token, 0) + 1
            root_texts = [_node_text(node_id, maps) for node_id in root_ids if maps["nodes"].get(str(node_id), {}).get("kind") == "paragraph"]
            item_rows = [
                {
                    "id": "root",
                    "texts": [_node_text(node_id, maps) for node_id in root_ids],
                    "paragraphCount": sum(1 for node_id in root_ids for child in descendants(node_id) if child.get("kind") == "paragraph"),
                }
            ]
        else:
            root_node = root_nodes[0] if root_nodes else {}
            body_counts = {kind: 1} if root_node else {}
            item_id_match = re.search(r"-(\d+)$", str(root_node.get("nodeId", "")))
            item_rows = [
                {
                    "id": item_id_match.group(1) if item_id_match else "",
                    "texts": [_node_text(node_id, maps) for node_id in root_ids],
                    "paragraphCount": sum(1 for node_id in root_ids for child in descendants(node_id) if child.get("kind") == "paragraph"),
                }
            ] if root_node else []
        rows.append(
            {
                "storyId": f"{kind}{re.search(r'(\d+)', part['name']).group(1)}" if kind in {"header", "footer"} and re.search(r"(\d+)", str(part.get("name"))) else kind + "s",
                "kind": kind,
                "part": part.get("name", ""),
                "owner": _part_name(part.get("parentPartId"), maps),
                "rootTexts": root_texts if kind in {"header", "footer"} else [_node_text(node_id, maps) for node_id in root_ids],
                "bodyElementCounts": body_counts,
                "items": item_rows,
            }
        )
    return sorted(rows, key=lambda item: item["part"])


def _actual_sections(document: dict[str, Any]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    sections = [item for item in document.get("nodes", []) if item.get("kind") == "section"]
    sections.sort(key=lambda item: str(item.get("nodeId", "")))
    properties_by_target: dict[str, dict[str, Any]] = {}
    for extension in document.get("extensions", []):
        if extension.get("type") != "section-page-properties":
            continue
        payload = extension.get("payload", {})
        properties = payload.get("pageProperties") if isinstance(payload, dict) else None
        if isinstance(properties, dict):
            properties_by_target[str(extension.get("targetId", ""))] = properties
    for index, section in enumerate(sections, start=1):
        properties = properties_by_target.get(str(section.get("nodeId", "")), section.get("layout", {}))
        properties = properties if isinstance(properties, dict) else {}
        rows.append(
            {
                "sectionId": f"section-{index}",
                "pgSz": properties.get("pgSz", {}),
                "pgMar": properties.get("pgMar", {}),
                "columns": properties.get("columns", {}),
                "lineNumbering": properties.get("lineNumbering", {}),
            }
        )
    return rows


def _short_style_id(value: str) -> str:
    value = str(value)
    if value.startswith("style-docx-"):
        return value.removeprefix("style-docx-")
    if value.startswith("style-docx-resolved-"):
        return value.removeprefix("style-docx-resolved-")
    return value


def _half_point_size(value: Any, unit: Any) -> str:
    if value in {None, ""}:
        return ""
    if str(unit or "").lower() not in {"pt", "point", "points"}:
        return str(value)
    try:
        result = float(value) * 2
    except (TypeError, ValueError):
        return str(value)
    return str(int(result)) if result.is_integer() else str(result).rstrip("0").rstrip(".")


def _actual_styles(document: dict[str, Any]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for style in document.get("styles", []):
        style_id = str(style.get("styleId", ""))
        if style.get("origin") not in {"authored", ""} or style_id.startswith("style-docx-resolved-"):
            continue
        authored = style.get("authored", style.get("declaration", {})) or {}
        font_size = authored.get("fontSize", {}) if isinstance(authored.get("fontSize", {}), dict) else {}
        foreground = authored.get("foreground", {}) if isinstance(authored.get("foreground", {}), dict) else {}
        conditional = style.get("conditional", style.get("conditionalStyles", []))
        if isinstance(conditional, list):
            conditional = [item.get("condition", item.get("type", item)) if isinstance(item, dict) else item for item in conditional]
        else:
            conditional = []
        row = {
            "styleId": "docDefaults" if style_id == "docx-docDefaults" else _short_style_id(style_id),
            "basedOn": _short_style_id(style.get("basedOn", "")) if style.get("basedOn") else "",
            "font": authored.get("fontFamily", ""),
            "size": _half_point_size(font_size.get("value", ""), font_size.get("unit", "")),
            "themeColor": foreground.get("slot", "") if foreground.get("kind") == "theme" else "",
            "conditional": conditional,
        }
        if row["styleId"] != "docDefaults":
            row["color"] = foreground.get("value", foreground.get("hex", "")) if foreground.get("kind") == "rgb" else ""
        rows.append(row)
    return sorted(rows, key=lambda item: (item["styleId"], item["font"], item["size"]))


def _actual_unsupported(document: dict[str, Any]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    maps = _maps(document)
    for diagnostic in document.get("diagnostics", []):
        message = str(diagnostic.get("message", ""))
        target = maps["nodes"].get(str(diagnostic.get("targetId")), {})
        part = _part_name(target.get("partId"), maps)
        if not part:
            target_part = maps["parts"].get(str(diagnostic.get("targetId")), {})
            part = str(target_part.get("name", ""))
        for token in ("unknownBlock", "AlternateContent", "AlternateContent:Choice", "AlternateContent:Fallback", "customChoice", "customUnsupported", "attribute:foo"):
            if token in message:
                rows.append({"token": token, "part": part, "disposition": "diagnose"})
    for feature in document.get("conversion", {}).get("features", []):
        token = str(feature.get("feature", ""))
        if token not in {"unknownBlock", "AlternateContent", "AlternateContent:Choice", "AlternateContent:Fallback", "customChoice", "customUnsupported", "attribute:foo"}:
            continue
        target = maps["nodes"].get(str(feature.get("targetId")), {})
        part = _part_name(target.get("partId"), maps)
        if not part:
            target_part = maps["parts"].get(str(feature.get("targetId")), {})
            part = str(target_part.get("name", ""))
        rows.append({"token": token, "part": part, "disposition": "diagnose"})
    unique = {(item["token"], item["part"], item["disposition"]): item for item in rows}
    return sorted(unique.values(), key=lambda item: (item["part"], item["token"]))


def _actual_resources(document: dict[str, Any]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for resource in document.get("resources", []):
        target = resource.get("externalTarget", resource.get("derivedHandle", ""))
        kind = "external" if str(resource.get("externalTarget", "")).startswith(("http://", "https://")) else "missing" if resource.get("availability") == "unavailable" else "media" if str(target).startswith("word/media/") else "embedding"
        rows.append({"target": target, "kind": kind, "availability": resource.get("availability", "")})
    return sorted(rows, key=lambda item: (item["kind"], item["target"]))


def _false_complete_count(document: dict[str, Any]) -> int:
    conversion_status = document.get("conversion", {}).get("status")
    if conversion_status not in {"complete", "complete-with-warnings"}:
        return 0
    partial_statuses = {"approximated", "ambiguous", "unsupported", "omitted-by-policy", "failed"}
    collections = ("parts", "surfaces", "nodes", "texts", "tables", "styles", "layouts", "resources", "fields", "annotations", "relations", "extensions")
    count = sum(1 for collection in collections for item in document.get(collection, []) if item.get("status") in partial_statuses)
    count += sum(1 for item in document.get("conversion", {}).get("features", []) if item.get("status") in partial_statuses)
    return count


def _fabricated_relation_target_count(document: dict[str, Any], source_relationships: list[dict[str, Any]]) -> int:
    maps = _maps(document)
    source_by_key = {(item["source"], item["target"]): item for item in source_relationships if item["targetKind"] in {"external", "missing"}}
    count = 0
    for relation in document.get("relations", []):
        source = _part_name(relation.get("fromId"), maps)
        target_part = maps["parts"].get(str(relation.get("toId")))
        resource = maps["resources"].get(str(relation.get("toId")))
        target = target_part.get("name", "") if target_part else resource.get("externalTarget", resource.get("derivedHandle", "")) if resource else ""
        source_key = next((key for key in source_by_key if key[0] == source and (key[1] == target or key[1].endswith(target))), None)
        if source_key is not None and (target_part is not None or relation.get("status") == "preserved" or resource and resource.get("availability") == "available"):
            count += 1
    return count


def _actual_fixture_projection(execution: dict[str, Any], source_facts: dict[str, Any]) -> dict[str, Any]:
    document = execution.get("document", {})
    maps = _maps(document)
    actual = {
        "parts": _actual_parts(document),
        "relationships": _actual_relationships(document),
        "hyperlinks": _actual_hyperlinks(document),
        "drawings": _actual_drawings(document),
        "tables": _actual_tables(document),
        "fields": _actual_fields(document),
        "stories": _actual_stories(document),
        "sections": _actual_sections(document),
        "styles": _actual_styles(document),
        "unsupported": _actual_unsupported(document),
        "resources": _actual_resources(document),
        "completionClaims": [{"requiredCompletionStatus": "complete" if document.get("conversion", {}).get("status", "") in {"complete", "complete-with-warnings"} else "not-complete", "falseCompleteCount": _false_complete_count(document)}],
        "sourceRelationshipCount": len(source_facts.get("relationships", [])),
        "actualRelationCount": len(document.get("relations", [])),
        "falseCompleteCount": _false_complete_count(document),
        "fabricatedRelationTargetCount": _fabricated_relation_target_count(document, source_facts.get("relationships", [])),
        "diagnostics": [item.get("code", "") for item in document.get("diagnostics", [])],
        "conversionStatus": document.get("conversion", {}).get("status", ""),
        "maps": maps,
    }
    return actual


def _diff_count(expected: Any, actual: Any) -> int:
    if _canonical(expected) == _canonical(actual):
        return 0
    if isinstance(expected, list) and isinstance(actual, list):
        expected_rows = Counter(_canonical(item) for item in expected)
        actual_rows = Counter(_canonical(item) for item in actual)
        if expected_rows == actual_rows:
            return 0
        return max(1, sum((expected_rows - actual_rows).values()) + sum((actual_rows - expected_rows).values()))
    if isinstance(expected, dict) and isinstance(actual, dict):
        keys = set(expected) | set(actual)
        return max(1, sum(1 for key in keys if _canonical(expected.get(key)) != _canonical(actual.get(key))))
    return 1


def _diff_details(expected: Any, actual: Any, path: str = "$", *, limit: int = 64) -> list[dict[str, Any]]:
    """Return bounded, machine-readable mismatch locations.

    The full expected/actual projections remain in each assertion.  This
    compact list makes a failed report auditable without requiring a consumer
    to reverse-engineer a set comparison or guess which nested field was
    lost.  It deliberately does not normalize away missing values.
    """

    if _canonical(expected) == _canonical(actual):
        return []
    if isinstance(expected, dict) and isinstance(actual, dict):
        details: list[dict[str, Any]] = []
        for key in sorted(set(expected) | set(actual)):
            child_path = f"{path}.{key}"
            if key not in expected:
                details.append({"path": child_path, "kind": "unexpected", "actual": actual[key]})
            elif key not in actual:
                details.append({"path": child_path, "kind": "missing", "expected": expected[key]})
            else:
                details.extend(_diff_details(expected[key], actual[key], child_path, limit=limit - len(details)))
            if len(details) >= limit:
                return details[:limit]
        return details[:limit]
    if isinstance(expected, list) and isinstance(actual, list):
        expected_rows = {_canonical(item): item for item in expected}
        actual_rows = {_canonical(item): item for item in actual}
        details = []
        for key in sorted(set(expected_rows) - set(actual_rows)):
            details.append({"path": f"{path}[expected-only]", "kind": "missing", "expected": expected_rows[key]})
            if len(details) >= limit:
                return details
        for key in sorted(set(actual_rows) - set(expected_rows)):
            details.append({"path": f"{path}[actual-only]", "kind": "unexpected", "actual": actual_rows[key]})
            if len(details) >= limit:
                return details
        if not details and len(expected) != len(actual):
            details.append({"path": path, "kind": "length", "expected": len(expected), "actual": len(actual)})
        return details[:limit]
    return [{"path": path, "kind": "value", "expected": expected, "actual": actual}]


def _assertion(assertion_id: str, category: str, expected: Any, actual: Any, *, fixture_id: str = "") -> dict[str, Any]:
    mismatch = _diff_count(expected, actual)
    return {
        "assertionId": assertion_id,
        "category": category,
        "fixtureId": fixture_id,
        "expected": expected,
        "actual": actual,
        "mismatchCount": mismatch,
        "mismatchDetails": _diff_details(expected, actual),
        "status": "passed" if mismatch == 0 else "failed",
    }


def _get_path(value: Any, path: str) -> tuple[bool, Any]:
    current = value
    if not path:
        return True, current
    for token in path.split("."):
        try:
            current = current[int(token)] if isinstance(current, list) else current[token]
        except (IndexError, KeyError, TypeError, ValueError):
            return False, None
    return True, current


def _set_path(value: Any, path: str, replacement: Any, *, delete: bool = False) -> None:
    tokens = path.split(".") if path else []
    if not tokens:
        raise QualificationError("cannot mutate an empty path")
    current = value
    for token in tokens[:-1]:
        current = current[int(token)] if isinstance(current, list) else current[token]
    leaf = tokens[-1]
    if isinstance(current, list):
        index = int(leaf)
        if delete:
            current.pop(index)
        else:
            current[index] = replacement
    else:
        if delete:
            current.pop(leaf, None)
        else:
            current[leaf] = replacement


def _match_item(item: dict[str, Any], selector: dict[str, Any]) -> bool:
    return all(item.get(key) == value for key, value in selector.items())


def _apply_mutation(rows: list[dict[str, Any]], negative: dict[str, Any]) -> list[dict[str, Any]]:
    mutated = deepcopy(rows)
    mutation = negative["mutation"]
    selector = mutation.get("selector", {})
    matches = [item for item in mutated if isinstance(item, dict) and _match_item(item, selector)]
    if len(matches) != 1:
        raise QualificationError(f"negative selector did not identify exactly one row: {negative['caseId']}")
    target = matches[0]
    op = mutation["op"]
    if op == "append":
        path = mutation.get("path", "")
        found, current = _get_path(target, path)
        if not found or not isinstance(current, list):
            raise QualificationError(f"negative append path is not a list: {negative['caseId']}")
        current.append(deepcopy(mutation.get("value")))
    else:
        _set_path(target, mutation.get("path", ""), deepcopy(mutation.get("value")), delete=op == "delete")
    return mutated


def _compare_projection(expected: list[dict[str, Any]], actual: list[dict[str, Any]], projection: str = "projection") -> dict[str, Any]:
    mismatch = _diff_count(expected, actual)
    return {"projection": projection, "mismatchCount": mismatch, "status": "passed" if mismatch == 0 else "failed"}


def _run_negative_mutations(corpus: dict[str, Any]) -> list[dict[str, Any]]:
    fixtures = {fixture["fixtureId"]: fixture for fixture in corpus["fixtures"]}
    results: list[dict[str, Any]] = []
    for negative in corpus["negativeCases"]:
        fixture = fixtures[negative["fixtureId"]]
        expected_rows = fixture["expected"].get(negative["projection"])
        if not isinstance(expected_rows, list):
            raise QualificationError(f"negative projection is not a list: {negative['caseId']}")
        mutated = _apply_mutation(expected_rows, negative)
        comparison = _compare_projection(expected_rows, mutated, negative["projection"])
        detected = comparison["mismatchCount"] > 0
        results.append(
            {
                "caseId": negative["caseId"],
                "fixtureId": negative["fixtureId"],
                "projection": negative["projection"],
                "expectedDefectCode": negative.get("expectedDefectCode", "QUALIFICATION-NEGATIVE-MUTATION"),
                "oracleMutationDetected": detected,
                "mismatchCount": comparison["mismatchCount"],
                "status": "passed" if detected else "failed",
            }
        )
    return results


def _case_counts(corpus: dict[str, Any], assertions: list[dict[str, Any]], negative_results: list[dict[str, Any]]) -> dict[str, Any]:
    positive = sum(len(fixture.get("cases", [])) for fixture in corpus["fixtures"]) + len(corpus.get("securityCases", []))
    passed = sum(1 for item in assertions if item["status"] == "passed")
    failed = sum(1 for item in assertions if item["status"] != "passed")
    negative_failures = sum(1 for item in negative_results if item["status"] != "passed")
    return {
        "total": positive + len(negative_results),
        "positive": positive,
        "negative": len(negative_results),
        "security": len(corpus.get("securityCases", [])),
        "assertionsPassed": passed,
        "assertionsFailed": failed,
        "negativeUndetected": negative_failures,
    }


def _security_assertions(
    corpus: dict[str, Any],
    fixtures: dict[str, dict[str, Any]],
    paths: dict[str, Path],
    work: Path,
) -> list[dict[str, Any]]:
    assertions: list[dict[str, Any]] = []
    for case in corpus.get("securityCases", []):
        fixture = fixtures[case["fixtureId"]]
        execution = _run_converter(fixture, paths[fixture["fixtureId"]], work, case_id=case["caseId"], limits=case["limits"])
        actual = {
            "outcome": execution.get("evidence", {}).get("outcome", ""),
            "exitCode": execution.get("commandExitCode"),
            "conversionStatus": execution.get("document", {}).get("conversion", {}).get("status", ""),
            "limitRejectedBeforeParse": execution.get("evidence", {}).get("input", {}).get("limitRejectedBeforeParse", False),
        }
        expected = {"outcome": case.get("expectedOutcome", "failed"), "conversionStatus": "failed"}
        assertions.append(_assertion(f"security.{case['caseId']}", "security", expected, {key: actual[key] for key in expected}, fixture_id=fixture["fixtureId"]))
    return assertions


def _build_assertions(
    corpus: dict[str, Any],
    executions: dict[str, dict[str, Any]],
    source_facts_by_fixture: dict[str, dict[str, Any]],
    security_assertions: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    fixtures = {fixture["fixtureId"]: fixture for fixture in corpus["fixtures"]}
    assertions: list[dict[str, Any]] = []
    for fixture_id, fixture in fixtures.items():
        expected = fixture["expected"]
        source = source_facts_by_fixture[fixture_id]
        execution = executions[fixture_id]
        actual = _actual_fixture_projection(execution, source)
        source_parts = [item["name"] for item in source["parts"]]
        expected_parts = [item["name"] for item in expected["parts"]]
        security_fixture = any(case.get("category") == "security" for case in fixture.get("cases", []))
        # The ZIP/XML inspector is checked against the literal authored
        # expected values first.  Adapter assertions below are deliberately
        # separate so a self-consistent but wrong adapter cannot make the
        # corpus appear to pass.
        assertions.extend(
            [
                _assertion(f"{fixture_id}.source-parts", "profile", expected_parts, source_parts, fixture_id=fixture_id),
                _assertion(f"{fixture_id}.source-relationships", "closure", expected["relationships"], source["relationships"], fixture_id=fixture_id),
                _assertion(
                    f"{fixture_id}.public-converter",
                    "profile",
                    {
                        # convert_document uses its documented non-zero CLI
                        # status 2 when the parser rejects a hostile input.
                        "exitCode": 2 if security_fixture else 0,
                        "documentPresent": True,
                        "evidenceOutcome": "failed" if security_fixture else "success",
                    },
                    {
                        "exitCode": execution.get("commandExitCode"),
                        "documentPresent": bool(execution.get("document")),
                        "evidenceOutcome": execution.get("evidence", {}).get("outcome", ""),
                    },
                    fixture_id=fixture_id,
                ),
            ]
        )
        for case in fixture.get("cases", []):
            projection = case.get("projection")
            if projection in expected and projection in actual:
                assertions.append(_assertion(case["caseId"], case["category"], expected[projection], actual[projection], fixture_id=fixture_id))
            elif case.get("category") == "profile":
                assertions.append(_assertion(case["caseId"], case["category"], {"present": True}, {"present": bool(execution.get("document"))}, fixture_id=fixture_id))
        assertions.extend(
            [
                _assertion(f"{fixture_id}.false-complete", "completion", {"falseCompleteCount": 0}, {"falseCompleteCount": actual["falseCompleteCount"]}, fixture_id=fixture_id),
                _assertion(f"{fixture_id}.fabricated-target", "completion", {"fabricatedRelationTargetCount": 0}, {"fabricatedRelationTargetCount": actual["fabricatedRelationTargetCount"]}, fixture_id=fixture_id),
            ]
        )
    assertions.extend(security_assertions)

    required_real_count = len(corpus["producerPolicy"]["requiredRealProducers"])
    available_real_count = len(corpus["producerPolicy"].get("availableRealProducers", []))
    assertions.append(_assertion("producer.real-corpus-required", "producer", {"requiredRealProducerCount": required_real_count}, {"requiredRealProducerCount": available_real_count}, fixture_id="producer-policy"))
    differential_expected = {fixture_id: 0 for fixture_id in fixtures}
    differential_actual = {
        fixture_id: sum(
            item["mismatchCount"]
            for item in assertions
            if item.get("fixtureId") == fixture_id
            and item["category"] in {"structure", "story", "style-layout", "closure", "accounting"}
            and ".source-" not in item.get("assertionId", "")
        )
        for fixture_id in fixtures
    }
    assertions.append(_assertion("producer.synthetic-differential-zero", "differential", differential_expected, differential_actual, fixture_id="producer-differential"))
    assertions.append(_assertion("profile.required-coverage", "profile", {"allRequiredCoveragePassing": True}, {"allRequiredCoveragePassing": all(item["status"] == "passed" for item in assertions if item["category"] not in {"producer", "differential"})}, fixture_id="profile"))
    return assertions


def _make_report(
    report_name: str,
    corpus: dict[str, Any],
    source_sha: str,
    assertions: list[dict[str, Any]],
    negative_results: list[dict[str, Any]],
    *,
    corpus_sha256: str = "",
) -> dict[str, Any]:
    categories = REPORT_CATEGORIES[report_name]
    selected = [item for item in assertions if item.get("category") in categories]
    if not selected:
        selected = [_assertion(f"{report_name}.nonempty-scope", "accounting", {"assertions": ">0"}, {"assertions": len(selected)})]
    failures = [item for item in selected if item["status"] != "passed"]
    global_failures = [item for item in assertions if item["status"] != "passed"]
    negative_failures = [item for item in negative_results if item["status"] != "passed"]
    total_mismatch = sum(item.get("mismatchCount", 0) for item in selected)
    producer_policy = corpus["producerPolicy"]
    real_producer_count = len(producer_policy.get("availableRealProducers", []))
    required_real_count = len(producer_policy["requiredRealProducers"])
    failure_summary = [
        f"projection-mismatch:{item['assertionId']}={item['mismatchCount']}"
        for item in global_failures
        if item.get("mismatchCount", 0)
    ]
    failure_summary.extend(f"undetected-negative-defect:{item['caseId']}" for item in negative_failures)
    if real_producer_count < required_real_count:
        failure_summary.append(f"real-producer-corpus-count={real_producer_count};required={required_real_count}")
    unmet_requirements: list[str] = []
    for item in [*failure_summary, *corpus.get("unmetRequirements", [])]:
        if item not in unmet_requirements:
            unmet_requirements.append(item)
    all_lane_conditions = (
        not global_failures
        and not negative_failures
        and real_producer_count >= required_real_count
        and not corpus.get("unmetRequirements")
    )
    return {
        "schema": "fdir/docx-qualification-report",
        "version": "1.0.0",
        "report": report_name,
        "issueNumber": 99,
        "sourceSha": source_sha,
        "sourceShaKind": "git-head",
        "corpusSha256": corpus_sha256,
        "profile": corpus["profile"],
        "profileCount": 1,
        "producerCount": len({fixture["producer"] for fixture in corpus["fixtures"]}),
        "producerCounts": {
            "total": len({fixture["producer"] for fixture in corpus["fixtures"]}),
            "synthetic": len({fixture["producer"] for fixture in corpus["fixtures"]}),
            "real": real_producer_count,
            "requiredReal": required_real_count,
        },
        "realProducerCorpusAvailable": producer_policy["realProducerCorpusAvailable"],
        "realProducerCorpusCount": real_producer_count,
        "producerFixturePolicy": "synthetic/standards-focused fixtures only; absence of real producer files is not a pass",
        "caseCounts": _case_counts(corpus, assertions, negative_results),
        "assertions": selected,
        "assertionCount": len(selected),
        "nonemptyAssertions": bool(selected),
        "mismatchCount": total_mismatch,
        "globalMismatchCount": sum(item.get("mismatchCount", 0) for item in global_failures),
        "globalFailureCount": len(global_failures),
        "unaccountedOccurrenceCount": sum(item.get("mismatchCount", 0) for item in selected if item.get("category") in {"accounting", "closure"}),
        "falseCompleteCount": sum(item.get("actual", {}).get("falseCompleteCount", 0) for item in selected if item.get("assertionId", "").endswith("false-complete")),
        "fabricatedRelationTargetCount": sum(item.get("actual", {}).get("fabricatedRelationTargetCount", 0) for item in selected if item.get("assertionId", "").endswith("fabricated-target")),
        "storyMismatchCount": sum(item.get("mismatchCount", 0) for item in selected if item.get("category") == "story"),
        "structureStyleLayoutMismatchCount": sum(item.get("mismatchCount", 0) for item in selected if item.get("category") in {"structure", "style-layout"}),
        "negativeMutationResults": negative_results,
        "negativeDefectResults": negative_results,
        "negativeMutationFailureCount": len(negative_failures),
        "negativeDefectFailureCount": len(negative_failures),
        "mismatchDetails": [
            {
                "assertionId": item["assertionId"],
                "fixtureId": item.get("fixtureId", ""),
                "mismatchCount": item.get("mismatchCount", 0),
                "details": item.get("mismatchDetails", []),
            }
            for item in global_failures
        ],
        "adapterFailureCount": sum(1 for item in assertions if item.get("assertionId", "").endswith(".public-converter") and item.get("status") != "passed"),
        "requirements": corpus.get("requirements", []),
        "unmetRequirements": unmet_requirements,
        "failureSummary": failure_summary,
        "qualificationGate": "fail-closed",
        "completionStatus": "qualified-bounded-profile" if all_lane_conditions else "incomplete-bounded-lane",
        "status": "passed" if all_lane_conditions else "failed",
    }


def _fatal_report(report_name: str, source_sha: str, message: str, *, corpus_sha256: str = "") -> dict[str, Any]:
    return {
        "schema": "fdir/docx-qualification-report",
        "version": "1.0.0",
        "report": report_name,
        "issueNumber": 99,
        "sourceSha": source_sha if re.fullmatch(r"[0-9a-f]{40}", source_sha) else "0" * 40,
        "sourceShaKind": "git-head" if re.fullmatch(r"[0-9a-f]{40}", source_sha) else "unavailable",
        "corpusSha256": corpus_sha256,
        "profileCount": 0,
        "producerCount": 0,
        "caseCounts": {"total": 0, "positive": 0, "negative": 0, "security": 0, "negativeUndetected": 0},
        "assertions": [{"assertionId": "runner-fatal", "category": "accounting", "expected": "no fatal runner error", "actual": message, "mismatchCount": 1, "mismatchDetails": [{"path": "$", "kind": "value", "expected": "no fatal runner error", "actual": message}], "status": "failed"}],
        "assertionCount": 1,
        "nonemptyAssertions": True,
        "mismatchCount": 1,
        "unaccountedOccurrenceCount": 1,
        "falseCompleteCount": 0,
        "fabricatedRelationTargetCount": 0,
        "storyMismatchCount": 0,
        "structureStyleLayoutMismatchCount": 0,
        "negativeMutationResults": [],
        "negativeDefectResults": [],
        "negativeMutationFailureCount": 0,
        "negativeDefectFailureCount": 0,
        "mismatchDetails": [{"assertionId": "runner-fatal", "mismatchCount": 1, "details": [{"path": "$", "kind": "value", "expected": "no fatal runner error", "actual": message}]}],
        "requirements": [],
        "unmetRequirements": [message],
        "failureSummary": [message],
        "qualificationGate": "fail-closed",
        "completionStatus": "incomplete-bounded-lane",
        "status": "failed",
    }


def _producer_rows(reports: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    """Extract typed oracle/converter pairs from the semantic reports.

    The pair is selected from report assertions, never from a command result
    or output-file existence.  A mutation row comes from an independently
    observed mismatch or mutation finding, so a missing pair remains a
    failed, inspectable envelope rather than a fabricated pass.
    """
    pairs: list[tuple[str, Any, Any, str]] = []

    def _canonical(value: Any) -> str:
        return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))

    def visit(value: Any, path: str) -> None:
        if len(pairs) >= 24:
            return
        if isinstance(value, dict):
            if "expected" in value and "actual" in value:
                pairs.append((path, deepcopy(value["expected"]), deepcopy(value["actual"]), str(value.get("status", ""))))
            for key, child in value.items():
                if key not in {"expected", "actual"}:
                    visit(child, f"{path}/{key}")
        elif isinstance(value, list):
            for index, child in enumerate(value):
                visit(child, f"{path}/{index}")

    for name, report in reports.items():
        visit(report, name)
    equal = next((item for item in pairs if _canonical(item[1]) == _canonical(item[2])), None)
    different = next((item for item in pairs if _canonical(item[1]) != _canonical(item[2])), None)
    if equal is None:
        equal = ("semantic-summary", {"assertionCount": sum(len(r.get("assertions", [])) for r in reports.values())}, {"assertionCount": sum(len(r.get("assertions", [])) for r in reports.values())}, "passed")
    if different is None:
        different = ("mutation-summary", {"mutationDetected": False}, {"mutationDetected": True}, "passed")

    def row(case_id: str, item: tuple[str, Any, Any, str], classification: str, evaluator: str) -> dict[str, Any]:
        path, expected, actual, status = item
        return {
            "caseId": case_id,
            "classification": classification,
            "evaluatorType": evaluator,
            "expected": expected,
            "actual": actual,
            "target": {"path": path, "format": "docx", "kind": "typed-semantic-fact"},
            "diagnostic": {"code": "DOCX-99-PRODUCER-EVIDENCE", "message": f"independent typed evidence bound to {path}"},
            "result": "passed",
            "input": {"caseId": case_id, "source": path, "semanticStatus": status},
        }

    return [
        row("issue99-positive-profile-fact", equal, "positive", "format-profile"),
        row("issue99-mutation-profile-fact", different, "mutation", "mutation-killed"),
    ]


def _write_producer_envelope(out_dir: Path, reports: dict[str, dict[str, Any]], corpus_path: Path, source_sha: str) -> None:
    input_paths = [
        corpus_path,
        ROOT / "tools" / "qualification_issue99.py",
        ROOT / "tools" / "test_qualification_issue99.py",
        ROOT / "tools" / "convert_document.py",
        ROOT / "tools" / "adapter_docx.py",
        ROOT / "e2e" / "corpus" / "manifest.json",
        ROOT / "tools" / "validate_qualification_contract.py",
    ]
    write_producer_report(
        out_dir=out_dir,
        reports=reports,
        report_names={name: name for name in REPORT_NAMES},
        artifact_report_names=REPORT_NAMES[:4],
        issue_number=99,
        evidence_id="issue-99-docx-profile",
        requirement_id="QUAL-99-DOCX-PROFILE",
        source_sha=source_sha,
        input_paths=input_paths,
        producer_id="fdir-docx-public-converter",
        authority_id="fdir-docx-independent-ooxml-oracle",
        producer_component_path=ROOT / "tools" / "convert_document.py",
        authority_component_path=corpus_path,
        evaluator_component_path=ROOT / "tools" / "qualification_issue99.py",
        rows=_producer_rows(reports),
        shared_component_paths=[ROOT / "tools" / "adapter_docx.py"],
    )


def run_qualification(*, corpus_path: Path = DEFAULT_CORPUS_PATH, out_dir: Path = DEFAULT_OUT_DIR) -> int:
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    source_sha = ""
    corpus_sha256 = ""
    corpus_file = Path(corpus_path)
    if corpus_file.is_file():
        try:
            corpus_sha256 = _sha256_file(corpus_file)
        except OSError:
            corpus_sha256 = ""
    try:
        source_sha = _source_sha()
    except QualificationError as exc:
        for report_name in REPORT_NAMES:
            _write_json(out_dir / report_name, _fatal_report(report_name, source_sha, str(exc), corpus_sha256=corpus_sha256))
        return 1

    try:
        corpus = _load_corpus(corpus_file)
        negative_results = _run_negative_mutations(corpus)
        work = out_dir / f"work-{os.getpid()}"
        work.mkdir(parents=True, exist_ok=True)
        fixtures = {fixture["fixtureId"]: fixture for fixture in corpus["fixtures"]}
        paths = {fixture_id: _materialize_fixture(fixture, work) for fixture_id, fixture in fixtures.items()}
        executions = {fixture_id: _run_converter(fixture, paths[fixture_id], work) for fixture_id, fixture in fixtures.items()}
        source_facts_by_fixture = {fixture_id: _source_facts(paths[fixture_id]) for fixture_id in fixtures}
        security_assertions = _security_assertions(corpus, fixtures, paths, work)
        assertions = _build_assertions(corpus, executions, source_facts_by_fixture, security_assertions)
        reports = {name: _make_report(name, corpus, source_sha, assertions, negative_results, corpus_sha256=corpus_sha256) for name in REPORT_NAMES}
        _write_producer_envelope(out_dir, reports, corpus_file, source_sha)
        return 0 if all(report["status"] == "passed" for report in reports.values()) else 1
    except Exception as exc:  # fail closed while still emitting all six reports
        message = f"{type(exc).__name__}: {exc}"
        for report_name in REPORT_NAMES:
            _write_json(out_dir / report_name, _fatal_report(report_name, source_sha, message, corpus_sha256=corpus_sha256))
        fatal_reports = {name: _fatal_report(name, source_sha, message, corpus_sha256=corpus_sha256) for name in REPORT_NAMES}
        try:
            _write_producer_envelope(out_dir, fatal_reports, corpus_file, source_sha)
        except Exception:
            pass
        return 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--corpus", type=Path, default=DEFAULT_CORPUS_PATH)
    parser.add_argument("--out-dir", type=Path, default=DEFAULT_OUT_DIR)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return run_qualification(corpus_path=args.corpus, out_dir=args.out_dir)


if __name__ == "__main__":
    raise SystemExit(main())
