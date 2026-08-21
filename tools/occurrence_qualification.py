"""Independent source-occurrence accounting for qualification issue #91.

The public adapters are deliberately *not* imported here.  This module reads
the source bytes with small inspection-only parsers and then validates a
separate accounting payload against that independent enumeration.  It is
therefore possible for a handler to be disabled, an occurrence to be deleted,
or a locator to be swapped without the accounting lane silently following the
same adapter metadata.

The command writes four machine-readable reports when invoked as::

    python tools/occurrence_qualification.py qualify INPUT --format FORMAT \
        --accounting ACCOUNTING.json --out-dir REPORT_DIR

An omitted accounting file is intentional fail-closed behaviour: every source
occurrence is reported as unaccounted and the command exits non-zero.
"""

from __future__ import annotations

import argparse
from collections import Counter, defaultdict
import hashlib
import json
from pathlib import Path
import re
import sys
from typing import Any, Iterable, Sequence
import xml.etree.ElementTree as ET
import zipfile


ROOT = Path(__file__).resolve().parents[1]
CAPABILITY_PROFILE_PATH = ROOT / "machine" / "capability-profile.json"
REPORT_NAMES = (
    "source-occurrence-accounting.json",
    "capability-profile-coverage.json",
    "status-aggregation.json",
    "unaccounted-occurrences.json",
)
DISPOSITIONS = frozenset(
    {
        "preserved",
        "normalized",
        "approximated",
        "ambiguous",
        "unsupported",
        "omitted-by-policy",
        "unavailable-observation",
        "failed",
    }
)
COMPLETE_DISPOSITIONS = frozenset({"preserved", "normalized"})
REQUIRED_OCCURRENCE_FIELDS = (
    "sourceOccurrenceId",
    "formatProfileId",
    "sourceLocator",
    "constructId",
    "parentOccurrenceId",
    "sourceDigest",
    "tokenDigest",
    "policyRuleId",
    "evidenceCaseId",
)
KNOWN_XML_LOCALS = {
    "AlternateContent",
    "Choice",
    "Fallback",
    "Relationships",
    "Relationship",
    "Types",
    "Default",
    "Override",
    "document",
    "body",
    "p",
    "pPr",
    "r",
    "rPr",
    "rFonts",
    "t",
    "br",
    "tbl",
    "tr",
    "tc",
    "sectPr",
    "headerReference",
    "footerReference",
    "style",
    "styles",
    "latentStyles",
    "numPr",
    "drawing",
    "inline",
    "extent",
    "docPr",
    "graphic",
    "graphicData",
    "header",
    "hdr",
    "footer",
    "ftr",
    "footnotes",
    "endnotes",
    "comment",
    "footnoteReference",
    "endnoteReference",
    "workbook",
    "sheets",
    "sheet",
    "worksheet",
    "sheetData",
    "row",
    "c",
    "v",
    "f",
    "is",
    "mergeCells",
    "mergeCell",
    "dimension",
    "table",
    "tableParts",
    "tablePart",
    "definedNames",
    "definedName",
    "calcPr",
    "extLst",
    "ext",
    "externalLinks",
    "externalLink",
    "pivotTableDefinition",
    "pivotField",
    "pivotFields",
    "pivotCacheDefinition",
    "cacheSource",
    "location",
}
KNOWN_XML_ATTRS = {
    "id",
    "Id",
    "type",
    "Type",
    "target",
    "Target",
    "TargetMode",
    "PartName",
    "ContentType",
    "Extension",
    "r",
    "t",
    "style",
    "spans",
    "ref",
    "name",
    "displayName",
    "uniqueCount",
    "count",
    "sheetId",
    "state",
    "showGridLines",
    "code",
    "uri",
    "version",
    "encoding",
    "author",
    "date",
    "space",
    "val",
    "w",
}
PDF_OPERATORS = {
    "b",
    "B",
    "B*",
    "BDC",
    "BI",
    "BMC",
    "BT",
    "BX",
    "CS",
    "Do",
    "DP",
    "EI",
    "EMC",
    "ET",
    "EX",
    "F",
    "G",
    "J",
    "K",
    "M",
    "MP",
    "Q",
    "RG",
    "RI",
    "SC",
    "SCN",
    "T*",
    "TD",
    "Tf",
    "TJ",
    "TL",
    "Tc",
    "Td",
    "Tj",
    "Tm",
    "Tr",
    "Ts",
    "Tw",
    "Tz",
    "W",
    "W*",
    "b*",
    "c",
    "cm",
    "cs",
    "d",
    "d0",
    "d1",
    "f",
    "f*",
    "g",
    "gs",
    "h",
    "i",
    "j",
    "k",
    "l",
    "m",
    "n",
    "q",
    "re",
    "rg",
    "ri",
    "s",
    "sc",
    "scn",
    "sh",
    "v",
    "w",
    "y",
    "'",
    '"',
}
PDF_NON_OPERATORS = {"R", "true", "false", "null"}


def _bounded_rules(format_name: str) -> list[dict[str, Any]]:
    """Return the deliberately incomplete local rule skeleton.

    The checked-in capability profile predates #91 and has no
    construct-level policy yet.  Keeping this fallback in the new lane makes
    enumeration and negative testing useful immediately, while
    ``profileBound`` remains false and every report fails closed until the
    shared profile is redesigned.  These rules are not release evidence.
    """

    if format_name in {"docx", "xlsx"}:
        constructs = {
            "opc.package-part": ("supported", ["preserved", "normalized"], True, False),
            "opc.resource-part": ("supported", ["preserved", "normalized"], True, False),
            "opc.xml-element": ("diagnosed", ["unsupported", "approximated", "ambiguous", "failed"], False, True),
            "opc.xml-attribute": ("diagnosed", ["unsupported", "approximated", "ambiguous", "failed"], False, True),
            "opc.xml-text": ("diagnosed", ["unsupported", "approximated", "ambiguous", "failed"], False, True),
            "opc.relationship": ("diagnosed", ["preserved", "normalized", "unsupported", "unavailable-observation", "failed"], True, True),
            "opc.relationship-attribute": ("diagnosed", ["preserved", "normalized", "unsupported", "failed"], False, True),
            "opc.content-type-declaration": ("diagnosed", ["omitted-by-policy", "unsupported", "failed"], False, True),
            "opc.extension-element": ("diagnosed", ["preserved", "normalized", "unsupported", "failed"], False, True),
            "opc.xml-comment": ("diagnosed", ["omitted-by-policy", "unsupported", "failed"], False, True),
            "opc.xml-parse-failure": ("diagnosed", ["failed"], False, True),
            "opc.unreadable-input": ("diagnosed", ["failed"], False, True),
        }
    elif format_name == "pdf":
        constructs = {
            "pdf.indirect-object": ("supported", ["preserved", "normalized"], True, False),
            "pdf.indirect-reference": ("supported", ["preserved", "normalized", "unsupported", "unavailable-observation", "failed"], True, True),
            "pdf.page": ("supported", ["preserved", "normalized"], True, False),
            "pdf.resource-entry": ("supported", ["preserved", "normalized", "unsupported", "failed"], True, True),
            "pdf.annotation-action": ("diagnosed", ["preserved", "normalized", "unsupported", "ambiguous", "failed"], True, True),
            "pdf.font-cmap": ("diagnosed", ["preserved", "normalized", "unavailable-observation", "unsupported", "failed"], True, True),
            "pdf.content-operator": ("supported", ["preserved", "normalized", "unsupported", "failed"], True, True),
            "pdf.inline-image": ("diagnosed", ["preserved", "normalized", "unsupported", "failed"], True, True),
            "pdf.xref-entry": ("diagnosed", ["preserved", "normalized", "unavailable-observation", "failed"], False, True),
            "pdf.revision": ("diagnosed", ["unavailable-observation", "unsupported", "failed"], False, True),
            "pdf.encryption-filter": ("diagnosed", ["unavailable-observation", "unsupported", "failed"], False, True),
            "pdf.unreadable-input": ("diagnosed", ["failed"], False, True),
        }
    else:
        constructs = {
            "markdown.source-span": ("supported", ["preserved", "normalized"], True, False),
            "markdown.block": ("supported", ["preserved", "normalized"], True, False),
            "markdown.inline-token": ("supported", ["preserved", "normalized"], True, False),
            "markdown.delimiter": ("supported", ["preserved", "normalized", "unsupported", "ambiguous", "failed"], True, True),
            "markdown.reference-definition": ("supported", ["preserved", "normalized", "unsupported", "failed"], True, True),
            "markdown.reference-use": ("supported", ["preserved", "normalized", "unsupported", "failed"], True, True),
            "markdown.raw-html": ("diagnosed", ["preserved", "normalized", "unsupported", "failed"], True, True),
            "markdown.entity": ("supported", ["preserved", "normalized", "unsupported", "failed"], True, True),
            "markdown.escape": ("supported", ["preserved", "normalized"], True, False),
            "markdown.dialect-extension": ("diagnosed", ["unsupported", "approximated", "ambiguous", "failed"], False, True),
            "markdown.invalid-sequence": ("diagnosed", ["unsupported", "ambiguous", "failed"], False, True),
            "markdown.unreadable-input": ("diagnosed", ["failed"], False, True),
        }
    return [
        {
            "id": f"{format_name}-occurrence-{construct.replace('.', '-')}",
            "constructId": construct,
            "support": support,
            "allowedDispositions": allowed,
            "targetRequired": target_required,
            "diagnosticRequired": diagnostic_required,
        }
        for construct, (support, allowed, target_required, diagnostic_required) in sorted(constructs.items())
    ]


class OccurrenceQualificationError(ValueError):
    """Raised for malformed source, profile, or accounting data."""


def _canonical(value: Any) -> str:
    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise OccurrenceQualificationError(f"value is not canonical JSON: {exc}") from exc


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as stream:
            for block in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(block)
    except OSError as exc:
        raise OccurrenceQualificationError(f"cannot read source {path}: {exc}") from exc
    return digest.hexdigest()


def source_digest(path: Path) -> str:
    """Hash a file or package tree without depending on adapter helpers."""

    path = Path(path)
    if path.is_file():
        return _sha256_file(path)
    if not path.is_dir():
        raise OccurrenceQualificationError(f"source is not a regular file or directory: {path}")
    digest = hashlib.sha256()
    files = sorted((item for item in path.rglob("*") if item.is_file()), key=lambda item: item.relative_to(path).as_posix())
    for child in files:
        relative = child.relative_to(path).as_posix().encode("utf-8")
        digest.update(len(relative).to_bytes(8, "big"))
        digest.update(relative)
        digest.update(bytes.fromhex(_sha256_file(child)))
    return digest.hexdigest()


def _bounded_token(value: Any, limit: int = 512) -> Any:
    if isinstance(value, str):
        return value[:limit]
    if isinstance(value, list):
        return [_bounded_token(item, limit) for item in value[:64]]
    if isinstance(value, dict):
        return {str(key): _bounded_token(child, limit) for key, child in list(value.items())[:64]}
    return value


def _token_digest(value: Any) -> str:
    return _sha256_bytes(_canonical(_bounded_token(value)).encode("utf-8"))


def _read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise OccurrenceQualificationError(f"cannot read JSON {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise OccurrenceQualificationError(f"JSON root must be an object: {path}")
    return value


def load_capability_profile(path: Path = CAPABILITY_PROFILE_PATH) -> dict[str, Any]:
    value = _read_json(Path(path))
    if value.get("schema") != "fdir/document-form-capability-profile":
        raise OccurrenceQualificationError("capability profile schema is invalid")
    profiles = value.get("profiles")
    if not isinstance(profiles, list) or not profiles:
        raise OccurrenceQualificationError("capability profile has no profiles")
    return value


def profile_for_format(format_name: str, path: Path = CAPABILITY_PROFILE_PATH) -> dict[str, Any]:
    profile_doc = load_capability_profile(path)
    profiles = [item for item in profile_doc["profiles"] if isinstance(item, dict) and item.get("format") == format_name]
    if len(profiles) != 1:
        raise OccurrenceQualificationError(f"capability profile is not unique for format {format_name!r}")
    profile = dict(profiles[0])
    occurrence_policy = profile.get("occurrenceAccounting")
    if not isinstance(occurrence_policy, dict):
        # The shared profile is intentionally not edited by this bounded
        # implementation slice.  Use a local rule skeleton for enumeration
        # and mutation tests, but bind every report to the missing shared
        # contract so it cannot be mistaken for release qualification.
        occurrence_policy = {
            "version": "unbound",
            "profileBound": False,
            "completeEligibleDispositions": sorted(COMPLETE_DISPOSITIONS),
            "rules": _bounded_rules(format_name),
        }
        profile["occurrenceAccounting"] = occurrence_policy
    rules = occurrence_policy.get("rules")
    if not isinstance(rules, list) or not rules:
        raise OccurrenceQualificationError(f"profile {profile.get('id')} has no occurrence rules")
    if occurrence_policy.get("profileBound") is True:
        required_fields = occurrence_policy.get("requiredOccurrenceFields")
        if required_fields != list(REQUIRED_OCCURRENCE_FIELDS):
            raise OccurrenceQualificationError(
                f"profile {profile.get('id')} required occurrence fields are not the closed contract"
            )
        complete_dispositions = occurrence_policy.get("completeEligibleDispositions")
        if not isinstance(complete_dispositions, list) or set(complete_dispositions) != set(COMPLETE_DISPOSITIONS) or len(complete_dispositions) != len(COMPLETE_DISPOSITIONS):
            raise OccurrenceQualificationError(
                f"profile {profile.get('id')} complete disposition policy is not canonical"
            )
        if occurrence_policy.get("unknownConstructDisposition") != "failed":
            raise OccurrenceQualificationError(
                f"profile {profile.get('id')} does not fail closed for unknown constructs"
            )
        vectors = occurrence_policy.get("requiredTestVectors")
        required_vectors = {"positive-supported", "unknown-construct", "handler-omission", "duplicate-accounting", "locator-identity", "missing-resource"}
        if not isinstance(vectors, list) or not required_vectors.issubset(vectors):
            raise OccurrenceQualificationError(
                f"profile {profile.get('id')} required occurrence test vectors are incomplete"
            )
    rule_ids: set[str] = set()
    for rule in rules:
        if not isinstance(rule, dict) or not isinstance(rule.get("id"), str) or not isinstance(rule.get("allowedDispositions"), list):
            raise OccurrenceQualificationError(f"profile {profile.get('id')} has a malformed occurrence rule")
        if rule["id"] in rule_ids:
            raise OccurrenceQualificationError(f"profile {profile.get('id')} has duplicate occurrence rule {rule['id']}")
        if occurrence_policy.get("profileBound") is True:
            if not isinstance(rule.get("constructId"), str) or not rule["constructId"]:
                raise OccurrenceQualificationError(f"profile {profile.get('id')} has a rule without constructId")
            if rule.get("support") not in {"supported", "diagnosed"}:
                raise OccurrenceQualificationError(f"profile {profile.get('id')} has an invalid support level")
            if not set(rule["allowedDispositions"]).issubset(DISPOSITIONS):
                raise OccurrenceQualificationError(f"profile {profile.get('id')} has an invalid disposition policy")
            if not isinstance(rule.get("targetRequired"), bool) or not isinstance(rule.get("diagnosticRequired"), bool):
                raise OccurrenceQualificationError(f"profile {profile.get('id')} has incomplete binding policy")
            diagnostic_code = rule.get("requiredDiagnosticCode")
            if rule["diagnosticRequired"] and not isinstance(diagnostic_code, str):
                raise OccurrenceQualificationError(f"profile {profile.get('id')} has a diagnosed rule without a diagnostic code")
        rule_ids.add(rule["id"])
    return profile


def _rule_map(profile: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {rule["id"]: rule for rule in profile["occurrenceAccounting"]["rules"]}


def _rules_by_construct(profile: dict[str, Any]) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for rule in profile["occurrenceAccounting"]["rules"]:
        construct = rule.get("constructId")
        if not isinstance(construct, str) or construct in result:
            raise OccurrenceQualificationError(f"profile {profile.get('id')} has duplicate/missing constructId")
        result[construct] = rule
    return result


class _OccurrenceFactory:
    """Create occurrence records with insertion-resistant identity keys."""

    def __init__(self, profile: dict[str, Any], source_sha: str, evidence_case_id: str) -> None:
        self.profile = profile
        self.profile_id = str(profile["id"])
        self.source_sha = source_sha
        self.evidence_case_id = evidence_case_id
        self.rules = _rules_by_construct(profile)
        self.ordinals: Counter[str] = Counter()
        self.records: list[dict[str, Any]] = []

    def add(
        self,
        construct_id: str,
        locator: dict[str, Any],
        *,
        stable_locator: Any,
        raw_name: str | None = None,
        namespace: str | None = None,
        operator: str | None = None,
        parent_occurrence_id: str | None = None,
        token: Any = None,
    ) -> str:
        token_value = _bounded_token(token if token is not None else {"locator": stable_locator, "rawName": raw_name})
        token_sha = _token_digest(token_value)
        base = _canonical(
            {
                "profile": self.profile_id,
                "construct": construct_id,
                "stableLocator": stable_locator,
                "rawName": raw_name,
                "namespace": namespace,
                "operator": operator,
                "tokenDigest": token_sha,
            }
        )
        ordinal = self.ordinals[base]
        self.ordinals[base] += 1
        occurrence_id = "source-occurrence:" + _sha256_bytes((base + f":{ordinal}").encode("utf-8"))[:32]
        rule = self.rules.get(construct_id)
        record = {
            "sourceOccurrenceId": occurrence_id,
            "formatProfileId": self.profile_id,
            "sourceLocator": locator,
            "constructId": construct_id,
            "rawName": raw_name,
            "namespace": namespace,
            "operator": operator,
            "parentOccurrenceId": parent_occurrence_id,
            "sourceDigest": self.source_sha,
            "tokenDigest": token_sha,
            "policyRuleId": rule["id"] if rule else "unknown-construct",
            "evidenceCaseId": self.evidence_case_id,
            "disposition": None,
            "targetIds": [],
            "diagnosticIds": [],
            "accountingState": "unaccounted",
        }
        self.records.append(record)
        return occurrence_id


def _xml_name(tag: Any) -> tuple[str, str | None]:
    if not isinstance(tag, str):
        return "#comment", None
    if tag.startswith("{") and "}" in tag:
        namespace, local = tag[1:].split("}", 1)
        return local, namespace
    return tag, None


def _xml_construct(local: str, *, relationship_part: bool, content_types_part: bool) -> str:
    if local == "#comment":
        return "opc.xml-comment"
    if relationship_part and local == "Relationship":
        return "opc.relationship"
    if content_types_part and local in {"Default", "Override"}:
        return "opc.content-type-declaration"
    if local in {"AlternateContent", "Choice", "Fallback", "extLst", "ext"}:
        return "opc.extension-element"
    if local not in KNOWN_XML_LOCALS:
        return "opc.unknown-element"
    return "opc.xml-element"


def _xml_attribute_construct(local: str) -> str:
    return "opc.xml-attribute" if local in KNOWN_XML_ATTRS else "opc.unknown-attribute"


def _package_members(path: Path) -> dict[str, bytes]:
    path = Path(path)
    if path.is_dir():
        result: dict[str, bytes] = {}
        for child in sorted((item for item in path.rglob("*") if item.is_file()), key=lambda item: item.relative_to(path).as_posix()):
            name = child.relative_to(path).as_posix()
            if name in result:
                raise OccurrenceQualificationError(f"duplicate package member: {name}")
            try:
                result[name] = child.read_bytes()
            except OSError as exc:
                raise OccurrenceQualificationError(f"cannot read package member {name}: {exc}") from exc
        return result
    try:
        with zipfile.ZipFile(path) as archive:
            result = {}
            for info in archive.infolist():
                name = info.filename.replace("\\", "/")
                if name in result:
                    raise OccurrenceQualificationError(f"duplicate package member: {name}")
                result[name] = archive.read(info)
            return dict(sorted(result.items()))
    except (OSError, zipfile.BadZipFile, KeyError, RuntimeError) as exc:
        raise OccurrenceQualificationError(f"cannot inspect package: {exc}") from exc


def _xml_tree(data: bytes, member: str) -> ET.Element:
    try:
        parser = ET.XMLParser(target=ET.TreeBuilder(insert_comments=True, insert_pis=True))
        return ET.fromstring(data, parser=parser)
    except (ET.ParseError, ValueError) as exc:
        raise OccurrenceQualificationError(f"XML parse failed for {member}: {exc}") from exc


def _walk_xml(
    element: ET.Element,
    member: str,
    factory: _OccurrenceFactory,
    *,
    parent_id: str | None,
    path: str,
    relationship_part: bool,
    content_types_part: bool,
) -> None:
    local, namespace = _xml_name(element.tag)
    construct = _xml_construct(local, relationship_part=relationship_part, content_types_part=content_types_part)
    locator = {"part": member, "xmlPath": path}
    if construct == "opc.relationship":
        locator.update(
            {
                "relationshipId": element.attrib.get("Id"),
                "target": element.attrib.get("Target"),
                "targetMode": element.attrib.get("TargetMode", "Internal"),
            }
        )
    element_id = factory.add(
        construct,
        locator,
        stable_locator={"part": member, "xmlPath": path},
        raw_name=local,
        namespace=namespace,
        parent_occurrence_id=parent_id,
        token={"tag": element.tag, "attributes": sorted((str(k), str(v)) for k, v in element.attrib.items())},
    )
    for attribute_name, attribute_value in sorted(element.attrib.items(), key=lambda item: str(item[0])):
        attr_local, attr_namespace = _xml_name(attribute_name)
        attr_construct = "opc.relationship-attribute" if construct == "opc.relationship" else _xml_attribute_construct(attr_local)
        factory.add(
            attr_construct,
            {"part": member, "xmlPath": path, "attribute": attr_local},
            stable_locator={"part": member, "xmlPath": path, "attribute": attr_local},
            raw_name=attr_local,
            namespace=attr_namespace,
            parent_occurrence_id=element_id,
            token={"name": attribute_name, "value": attribute_value},
        )
    if element.text is not None and element.text != "":
        factory.add(
            "opc.xml-text",
            {"part": member, "xmlPath": path, "text": "element"},
            stable_locator={"part": member, "xmlPath": path, "text": "element"},
            raw_name="#text",
            parent_occurrence_id=element_id,
            token=element.text,
        )
    child_counts: Counter[str] = Counter()
    for child in list(element):
        child_local, _ = _xml_name(child.tag)
        child_counts[child_local] += 1
        child_path = f"{path}/{child_local}[{child_counts[child_local]}]"
        _walk_xml(
            child,
            member,
            factory,
            parent_id=element_id,
            path=child_path,
            relationship_part=relationship_part,
            content_types_part=content_types_part,
        )
        if child.tail is not None and child.tail != "":
            factory.add(
                "opc.xml-text",
                {"part": member, "xmlPath": child_path, "text": "tail"},
                stable_locator={"part": member, "xmlPath": child_path, "text": "tail"},
                raw_name="#tail",
                parent_occurrence_id=element_id,
                token=child.tail,
            )


def enumerate_opc(path: Path, format_name: str, factory: _OccurrenceFactory) -> None:
    try:
        members = _package_members(path)
    except OccurrenceQualificationError as exc:
        factory.add(
            "opc.unreadable-input",
            {"path": Path(path).name, "error": str(exc)},
            stable_locator={"path": Path(path).name, "kind": "unreadable-input"},
            raw_name=Path(path).name,
            token=str(exc),
        )
        return
    part_ids: dict[str, str] = {}
    for name in sorted(members):
        construct = "opc.package-part"
        if "/media/" in f"/{name}" or name.startswith("xl/embeddings/") or name.startswith("word/embeddings/"):
            construct = "opc.resource-part"
        part_ids[name] = factory.add(
            construct,
            {"part": name, "bytes": len(members[name])},
            stable_locator={"part": name},
            raw_name=name,
            token={"name": name, "bytes": len(members[name])},
        )
    for name in sorted(members):
        is_xml = name.lower().endswith((".xml", ".rels")) or name == "[Content_Types].xml"
        if not is_xml:
            continue
        relationship_part = name.endswith(".rels") or name == "_rels/.rels"
        content_types_part = name == "[Content_Types].xml"
        try:
            root = _xml_tree(members[name], name)
        except OccurrenceQualificationError as exc:
            factory.add(
                "opc.xml-parse-failure",
                {"part": name, "error": str(exc)},
                stable_locator={"part": name, "kind": "xml-parse-failure"},
                raw_name=name,
                parent_occurrence_id=part_ids.get(name),
                token=str(exc),
            )
            continue
        root_local, _ = _xml_name(root.tag)
        _walk_xml(
            root,
            name,
            factory,
            parent_id=part_ids.get(name),
            path=f"/{root_local}[1]",
            relationship_part=relationship_part,
            content_types_part=content_types_part,
        )


_PDF_OBJECT_HEADER = re.compile(rb"(?m)(?P<number>\d+)\s+(?P<generation>\d+)\s+obj\b")
_PDF_REFERENCE = re.compile(rb"(?<![A-Za-z0-9])(?P<number>\d+)\s+(?P<generation>\d+)\s+R(?![A-Za-z0-9])")
_PDF_XREF_ENTRY = re.compile(rb"(?m)^\s*(?P<offset>\d{10})\s+(?P<generation>\d{5})\s+(?P<state>[fn])\s*$")
_PDF_NAME = re.compile(rb"/(?P<name>[A-Za-z0-9_.+#-]+)")


def _pdf_objects(data: bytes) -> list[tuple[re.Match[bytes], bytes, int, int]]:
    matches = list(_PDF_OBJECT_HEADER.finditer(data))
    result: list[tuple[re.Match[bytes], bytes, int, int]] = []
    for index, match in enumerate(matches):
        end = matches[index + 1].start() if index + 1 < len(matches) else len(data)
        marker = data.find(b"endobj", match.end(), end)
        payload_end = marker if marker >= 0 else end
        result.append((match, data[match.end():payload_end], match.start(), payload_end))
    return result


def _pdf_stream_tokens(payload: bytes, base_offset: int) -> Iterable[tuple[str, int, int]]:
    stream_markers = list(re.finditer(rb"\bstream\b", payload))
    for stream_marker in stream_markers:
        start = stream_marker.end()
        if payload[start:start + 2] == b"\r\n":
            start += 2
        elif payload[start:start + 1] in {b"\r", b"\n"}:
            start += 1
        end = payload.find(b"endstream", start)
        if end < 0:
            end = len(payload)
        data = payload[start:end]
        index = 0
        while index < len(data):
            byte = data[index:index + 1]
            if byte in b" \t\r\n\f\x00":
                index += 1
                continue
            if byte == b"%":
                newline = data.find(b"\n", index)
                index = len(data) if newline < 0 else newline + 1
                continue
            token_start = index
            if byte == b"(":
                depth = 1
                index += 1
                while index < len(data) and depth:
                    if data[index:index + 1] == b"\\":
                        index += 2
                        continue
                    if data[index:index + 1] == b"(":
                        depth += 1
                    elif data[index:index + 1] == b")":
                        depth -= 1
                    index += 1
                continue
            if byte == b"<" and data[index:index + 2] != b"<<":
                close = data.find(b">", index + 1)
                index = len(data) if close < 0 else close + 1
                continue
            if byte == b"/":
                match = re.match(rb"/[A-Za-z0-9_.+#-]+", data[index:])
                index += len(match.group(0)) if match else 1
                continue
            if byte in b"[]{}<>":
                index += 2 if data[index:index + 2] in {b"<<", b">>"} else 1
                continue
            while index < len(data) and data[index:index + 1] not in b" \t\r\n\f\x00[]{}<>()/%":
                index += 1
            if index == token_start:
                index += 1
                continue
            token = data[token_start:index].decode("latin-1", errors="replace")
            if token in PDF_NON_OPERATORS or re.fullmatch(r"[+-]?(?:\d+(?:\.\d*)?|\.\d+)", token):
                continue
            yield token, base_offset + start + token_start, base_offset + start + index


def enumerate_pdf(path: Path, factory: _OccurrenceFactory) -> None:
    try:
        data = Path(path).read_bytes()
    except OSError as exc:
        factory.add(
            "pdf.unreadable-input",
            {"path": Path(path).name, "error": str(exc)},
            stable_locator={"path": Path(path).name, "kind": "unreadable-input"},
            raw_name=Path(path).name,
            token=str(exc),
        )
        return
    objects = _pdf_objects(data)
    object_ids: dict[tuple[int, int], str] = {}
    for match, payload, start, end in objects:
        number = int(match.group("number"))
        generation = int(match.group("generation"))
        object_key = f"{number} {generation}"
        object_id = factory.add(
            "pdf.indirect-object",
            {"object": number, "generation": generation, "byteStart": start, "byteEnd": end},
            stable_locator={"object": object_key},
            raw_name=object_key,
            token={"object": object_key},
        )
        object_ids.setdefault((number, generation), object_id)
    for match, payload, start, end in objects:
        number = int(match.group("number"))
        generation = int(match.group("generation"))
        parent = object_ids.get((number, generation))
        object_key = f"{number} {generation}"
        reference_ordinal: Counter[str] = Counter()
        for reference in _PDF_REFERENCE.finditer(payload):
            target = f"{int(reference.group('number'))} {int(reference.group('generation'))}"
            ordinal = reference_ordinal[target]
            reference_ordinal[target] += 1
            factory.add(
                "pdf.indirect-reference",
                {
                    "fromObject": object_key,
                    "toObject": target,
                    "byteStart": start + reference.start(),
                    "byteEnd": start + reference.end(),
                },
                stable_locator={"fromObject": object_key, "toObject": target, "ordinal": ordinal},
                raw_name=target,
                parent_occurrence_id=parent,
                token=target,
            )
        if re.search(rb"/Type\s*/Page\b", payload):
            factory.add(
                "pdf.page",
                {"object": object_key},
                stable_locator={"object": object_key},
                raw_name="Page",
                parent_occurrence_id=parent,
                token="/Type /Page",
            )
        if re.search(rb"/(?:Font|XObject|ExtGState|ColorSpace|Pattern|Shading|Properties)\b", payload):
            for name_match in _PDF_NAME.finditer(payload):
                name = name_match.group("name").decode("latin-1", errors="replace")
                if name not in {"Font", "XObject", "ExtGState", "ColorSpace", "Pattern", "Shading", "Properties"}:
                    continue
                factory.add(
                    "pdf.resource-entry",
                    {"object": object_key, "name": name, "byteStart": start + name_match.start()},
                    stable_locator={"object": object_key, "name": name},
                    raw_name=name,
                    parent_occurrence_id=parent,
                    token=f"/{name}",
                )
        if re.search(rb"/Subtype\s*/(?:Link|Text|Widget|Annot)\b|/(?:A|AA)\b", payload):
            factory.add(
                "pdf.annotation-action",
                {"object": object_key},
                stable_locator={"object": object_key},
                raw_name="annotation/action",
                parent_occurrence_id=parent,
                token="annotation/action",
            )
        if re.search(rb"/Type\s*/Font\b|/ToUnicode\b|\bbegincmap\b|\bbeginbf(?:char|range)\b", payload):
            factory.add(
                "pdf.font-cmap",
                {"object": object_key},
                stable_locator={"object": object_key},
                raw_name="font-cmap",
                parent_occurrence_id=parent,
                token="font-cmap",
            )
        if re.search(rb"/(?:Encrypt|Filter)\b", payload):
            factory.add(
                "pdf.encryption-filter",
                {"object": object_key},
                stable_locator={"object": object_key},
                raw_name="encryption/filter",
                parent_occurrence_id=parent,
                token="encryption/filter",
            )
        stream_match = re.search(rb"/Length\s+(?P<length>\d+).*?\bstream\s*(?:\r\n|\r|\n)", payload, re.S)
        if stream_match:
            stream_start = stream_match.end()
            stream_end = payload.find(b"endstream", stream_start)
            declared_length = int(stream_match.group("length"))
            stream_bytes = payload[stream_start:stream_end if stream_end >= 0 else len(payload)]
            actual_length = len(stream_bytes)
            if stream_end < 0 or actual_length != declared_length:
                factory.add(
                    "pdf.invalid-sequence",
                    {"object": object_key, "kind": "stream-length", "declaredLength": declared_length, "actualLength": actual_length},
                    stable_locator={"object": object_key, "kind": "stream-length"},
                    raw_name="invalid-stream-length",
                    parent_occurrence_id=parent,
                    token={"declaredLength": declared_length, "actualLength": actual_length},
                )
        operator_ordinal: Counter[str] = Counter()
        is_cmap_stream = bool(re.search(rb"\bbegincmap\b|/CIDInit\b", payload))
        for operator, byte_start, byte_end in (() if is_cmap_stream else _pdf_stream_tokens(payload, start + len(match.group(0)))):
            ordinal = operator_ordinal[operator]
            operator_ordinal[operator] += 1
            construct = "pdf.inline-image" if operator == "BI" else "pdf.content-operator" if operator in PDF_OPERATORS else "pdf.unknown-operator"
            factory.add(
                construct,
                {"object": object_key, "operator": operator, "byteStart": byte_start, "byteEnd": byte_end},
                stable_locator={"object": object_key, "operator": operator, "ordinal": ordinal},
                raw_name=operator,
                operator=operator,
                parent_occurrence_id=parent,
                token=operator,
            )
    for match in _PDF_XREF_ENTRY.finditer(data):
        factory.add(
            "pdf.xref-entry",
            {"byteStart": match.start(), "byteEnd": match.end(), "offset": match.group("offset").decode(), "generation": match.group("generation").decode(), "state": match.group("state").decode()},
            stable_locator={"offset": match.group("offset").decode(), "generation": match.group("generation").decode(), "state": match.group("state").decode()},
            raw_name="xref",
            token=match.group(0).decode("latin-1", errors="replace"),
        )
    eof_count = len(re.findall(rb"%%EOF", data))
    if eof_count > 1:
        for ordinal in range(eof_count - 1):
            factory.add(
                "pdf.revision",
                {"revision": ordinal + 1, "totalRevisions": eof_count},
                stable_locator={"revision": ordinal + 1, "totalRevisions": eof_count},
                raw_name="incremental-revision",
                token={"revision": ordinal + 1, "totalRevisions": eof_count},
            )


def _markdown_block_construct(line: str, *, in_fence: bool) -> str:
    stripped = line.strip()
    if not stripped:
        return "markdown.source-span"
    if stripped == "---" or (stripped.startswith("---") and ":" in stripped):
        return "markdown.block"
    if stripped.startswith(("```", "~~~")):
        return "markdown.block"
    if stripped.startswith(":::") or re.match(r"^[-*+] \[[ xX]\]", stripped):
        return "markdown.dialect-extension"
    if stripped.startswith("#"):
        return "markdown.block"
    if stripped.startswith((">", "- ", "* ", "+ ")):
        return "markdown.block"
    if re.match(r"^\[[^]]+\]:", stripped):
        return "markdown.reference-definition"
    if "|" in stripped:
        return "markdown.block"
    if re.search(r"<[/!]?[A-Za-z][^>]*>", line):
        return "markdown.raw-html"
    return "markdown.block"


def _markdown_inline_tokens(line: str) -> Iterable[tuple[str, int, int, str]]:
    specials = set("*_~[]()!`\\&<>")
    index = 0
    while index < len(line):
        if line[index] == "\\" and index + 1 < len(line):
            yield "markdown.escape", index, index + 2, line[index:index + 2]
            index += 2
            continue
        if line[index] == "&":
            entity = re.match(r"&(?:#\d+|#x[0-9A-Fa-f]+|[A-Za-z][A-Za-z0-9]+);", line[index:])
            if entity:
                end = index + len(entity.group(0))
                yield "markdown.entity", index, end, line[index:end]
                index = end
                continue
        reference = re.match(r"\[[^]]+\](?:\([^)]*\)|\[[^]]*\])", line[index:])
        if reference:
            end = index + len(reference.group(0))
            yield "markdown.reference-use", index, end, line[index:end]
            index = end
            continue
        if line[index] == "<":
            html = re.match(r"<[^>]+>", line[index:])
            if html:
                end = index + len(html.group(0))
                yield "markdown.raw-html", index, end, line[index:end]
                index = end
                continue
        if line[index] in specials:
            yield "markdown.delimiter", index, index + 1, line[index]
            index += 1
            continue
        start = index
        while index < len(line) and line[index] not in specials:
            index += 1
        if index > start:
            yield "markdown.inline-token", start, index, line[start:index]


def enumerate_markdown(path: Path, factory: _OccurrenceFactory) -> None:
    try:
        data = Path(path).read_bytes()
        text = data.decode("utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        factory.add(
            "markdown.unreadable-input",
            {"path": Path(path).name, "error": str(exc)},
            stable_locator={"path": Path(path).name, "kind": "unreadable-input"},
            raw_name=Path(path).name,
            token=str(exc),
        )
        return
    lines = text.splitlines()
    if text.endswith(("\n", "\r")):
        lines.append("")
    in_fence = False
    fence_marker = ""
    for line_number, line in enumerate(lines, start=1):
        construct = _markdown_block_construct(line, in_fence=in_fence)
        if construct == "markdown.block" and line.strip().startswith(("```", "~~~")):
            marker = line.strip()[:3]
            if not in_fence:
                in_fence = True
                fence_marker = marker
            elif marker == fence_marker:
                in_fence = False
        if in_fence and not line.strip().startswith(("```", "~~~")):
            construct = "markdown.block"
        char_start = sum(len(item) + 1 for item in lines[: line_number - 1])
        block_id = factory.add(
            construct,
            {"lineStart": line_number, "lineEnd": line_number, "columnStart": 1, "columnEnd": len(line) + 1, "charStart": char_start, "charEnd": char_start + len(line)},
            stable_locator={"lineText": line, "construct": construct},
            raw_name=construct.removeprefix("markdown."),
            parent_occurrence_id=None,
            token=line,
        )
        token_ordinals: Counter[tuple[str, str]] = Counter()
        for token_construct, start, end, token in _markdown_inline_tokens(line):
            key = (token_construct, token)
            ordinal = token_ordinals[key]
            token_ordinals[key] += 1
            factory.add(
                token_construct,
                {"lineStart": line_number, "lineEnd": line_number, "columnStart": start + 1, "columnEnd": end + 1, "charStart": char_start + start, "charEnd": char_start + end},
                stable_locator={"lineText": line, "tokenConstruct": token_construct, "token": token, "ordinal": ordinal},
                raw_name=token,
                parent_occurrence_id=block_id,
                token=token,
            )
        if "\x00" in line:
            factory.add(
                "markdown.invalid-sequence",
                {"lineStart": line_number, "lineEnd": line_number, "columnStart": line.index("\x00") + 1, "columnEnd": line.index("\x00") + 2},
                stable_locator={"lineText": line, "kind": "nul"},
                raw_name="NUL",
                parent_occurrence_id=block_id,
                token="NUL",
            )
        if line.count("`") % 2:
            column = line.index("`") + 1
            factory.add(
                "markdown.invalid-sequence",
                {"lineStart": line_number, "lineEnd": line_number, "columnStart": column, "columnEnd": len(line) + 1, "kind": "unclosed-code-span"},
                stable_locator={"lineText": line, "kind": "unclosed-code-span"},
                raw_name="unclosed-code-span",
                parent_occurrence_id=block_id,
                token=line[column - 1 :],
            )
        if re.search(r"!\[[^\]]*\](?:\([^)]*$|\[[^]]*$)|(?<!\!)\[[^\]]*\](?:\([^)]*$|\[[^]]*$)", line):
            column = line.find("[") + 1
            factory.add(
                "markdown.invalid-sequence",
                {"lineStart": line_number, "lineEnd": line_number, "columnStart": max(column, 1), "columnEnd": len(line) + 1, "kind": "unclosed-link"},
                stable_locator={"lineText": line, "kind": "unclosed-link"},
                raw_name="unclosed-link",
                parent_occurrence_id=block_id,
                token=line[max(column - 1, 0) :],
            )
    if in_fence and lines:
        opening = next((index for index, line in enumerate(lines, start=1) if line.strip().startswith(("```", "~~~"))), 1)
        factory.add(
            "markdown.invalid-sequence",
            {"lineStart": opening, "lineEnd": len(lines), "reason": "unclosed-fence"},
            stable_locator={"openingLine": lines[opening - 1], "kind": "unclosed-fence"},
            raw_name="unclosed-fence",
            token={"openingLine": lines[opening - 1]},
        )


def enumerate_source(
    path: Path,
    format_name: str,
    *,
    evidence_case_id: str | None = None,
    capability_profile_path: Path = CAPABILITY_PROFILE_PATH,
) -> dict[str, Any]:
    if format_name not in {"docx", "xlsx", "pdf", "markdown"}:
        raise OccurrenceQualificationError(f"unsupported format: {format_name}")
    path = Path(path)
    profile = profile_for_format(format_name, capability_profile_path)
    source_sha = source_digest(path)
    case_id = evidence_case_id or path.stem
    factory = _OccurrenceFactory(profile, source_sha, case_id)
    if format_name in {"docx", "xlsx"}:
        enumerate_opc(path, format_name, factory)
    elif format_name == "pdf":
        enumerate_pdf(path, factory)
    else:
        enumerate_markdown(path, factory)
    if not factory.records:
        fallback_construct = {
            "docx": "opc.unreadable-input",
            "xlsx": "opc.unreadable-input",
            "pdf": "pdf.unreadable-input",
            "markdown": "markdown.unreadable-input",
        }[format_name]
        factory.add(
            fallback_construct,
            {"path": path.name, "reason": "enumerator-observed-no-occurrences"},
            stable_locator={"path": path.name, "kind": "empty-observation"},
            raw_name=path.name,
            token="empty-observation",
        )
    return {
        "schema": "fdir/independent-source-occurrence-enumeration",
        "version": "1.0.0",
        "issueNumber": 91,
        "format": format_name,
        "formatProfileId": profile["id"],
        "evidenceCaseId": case_id,
        "source": str(path),
        "sourceSha": source_sha,
        "sourceOccurrenceCount": len(factory.records),
        "sourceOccurrences": factory.records,
        "capabilityProfile": {
            "id": profile["id"],
            "version": profile.get("version"),
            "occurrenceAccountingVersion": profile["occurrenceAccounting"].get("version"),
        },
        "enumerator": {
            "name": "tools/occurrence_qualification.py",
            "sourceOfTruth": "raw source bytes/package members",
            "adapterModulesImported": [],
        },
    }


def _entity_ids(ir: dict[str, Any]) -> tuple[set[str], set[str]]:
    targets: set[str] = set()
    diagnostics: set[str] = set()
    for collection, values in ir.items():
        if not isinstance(values, list):
            continue
        for item in values:
            if not isinstance(item, dict):
                continue
            for key, value in item.items():
                if key == "diagnosticId" and isinstance(value, str):
                    diagnostics.add(value)
                elif key.endswith("Id") and isinstance(value, str):
                    targets.add(value)
    return targets, diagnostics


def _load_accounting(path: Path | None) -> list[dict[str, Any]]:
    if path is None:
        return []
    value = json.loads(Path(path).read_text(encoding="utf-8"))
    if isinstance(value, list):
        entries = value
    elif isinstance(value, dict):
        entries = value.get("accounting", value.get("sourceOccurrences"))
    else:
        entries = None
    if not isinstance(entries, list) or not all(isinstance(item, dict) for item in entries):
        raise OccurrenceQualificationError("accounting payload must be an array or an object with accounting/sourceOccurrences")
    return entries


def validate_accounting(
    enumeration: dict[str, Any],
    accounting: Sequence[dict[str, Any]],
    *,
    ir: dict[str, Any] | None = None,
    require_ir: bool = False,
) -> dict[str, Any]:
    """Validate one accounting payload against independent source records."""

    expected = enumeration.get("sourceOccurrences")
    if not isinstance(expected, list) or not expected:
        return {"status": "failed", "failures": [{"code": "SOURCE_OCCURRENCE_EMPTY"}], "checks": {}}
    expected_by_id = {item.get("sourceOccurrenceId"): item for item in expected if isinstance(item, dict) and isinstance(item.get("sourceOccurrenceId"), str)}
    failures: list[dict[str, Any]] = []
    if len(expected_by_id) != len(expected):
        failures.append({"code": "SOURCE_OCCURRENCE_ID_NOT_UNIQUE"})
    ids = [item.get("sourceOccurrenceId") for item in accounting if isinstance(item, dict)]
    counts = Counter(value for value in ids if isinstance(value, str))
    duplicates = sorted(identifier for identifier, count in counts.items() if count > 1)
    unknown_ids = sorted(identifier for identifier in counts if identifier not in expected_by_id)
    accounted_ids = set(counts) & set(expected_by_id)
    unaccounted_ids = sorted(set(expected_by_id) - accounted_ids)
    if duplicates:
        failures.append({"code": "DUPLICATE_OCCURRENCE_ACCOUNTING", "occurrenceIds": duplicates})
    if unknown_ids:
        failures.append({"code": "UNKNOWN_OCCURRENCE_ID", "occurrenceIds": unknown_ids})
    if unaccounted_ids:
        failures.append({"code": "UNACCOUNTED_OCCURRENCE", "occurrenceIds": unaccounted_ids})
    if require_ir and ir is None:
        failures.append({"code": "IR_REQUIRED_FOR_TARGET_BINDING"})
    ir_targets, ir_diagnostics = _entity_ids(ir or {})
    profile = profile_for_format(str(enumeration.get("format")))
    rules = _rules_by_construct(profile)
    if profile["occurrenceAccounting"].get("profileBound") is not True:
        failures.append({"code": "CAPABILITY_PROFILE_NOT_CONSTRUCT_CLOSED", "profileId": profile.get("id")})
    disposition_counts: Counter[str] = Counter()
    unknown_construct_ids: list[str] = []
    account_by_id: dict[str, dict[str, Any]] = {}
    for entry in accounting:
        if not isinstance(entry, dict):
            failures.append({"code": "ACCOUNTING_ENTRY_MALFORMED"})
            continue
        identifier = entry.get("sourceOccurrenceId")
        if not isinstance(identifier, str) or identifier not in expected_by_id:
            continue
        if identifier in account_by_id:
            continue
        account_by_id[identifier] = entry
        source = expected_by_id[identifier]
        construct = source.get("constructId")
        rule = rules.get(construct)
        if rule is None:
            unknown_construct_ids.append(identifier)
            failures.append({"code": "UNKNOWN_CONSTRUCT", "occurrenceId": identifier, "constructId": construct})
        for field in REQUIRED_OCCURRENCE_FIELDS:
            if field not in entry:
                failures.append({"code": "ACCOUNTING_FIELD_MISSING", "occurrenceId": identifier, "field": field})
            elif entry.get(field) != source.get(field):
                failures.append({"code": "ACCOUNTING_SOURCE_IDENTITY_MISMATCH", "occurrenceId": identifier, "field": field})
        for field in ("sourceLocator", "targetIds", "diagnosticIds"):
            if field not in entry:
                continue
            if field == "sourceLocator" and not isinstance(entry[field], dict):
                failures.append({"code": "SOURCE_LOCATOR_MALFORMED", "occurrenceId": identifier})
        disposition = entry.get("disposition")
        if disposition not in DISPOSITIONS:
            failures.append({"code": "INVALID_DISPOSITION", "occurrenceId": identifier, "disposition": disposition})
            continue
        disposition_counts[disposition] += 1
        target_ids = entry.get("targetIds", [])
        diagnostic_ids = entry.get("diagnosticIds", [])
        if not isinstance(target_ids, list) or any(not isinstance(value, str) or not value for value in target_ids):
            failures.append({"code": "TARGET_IDS_MALFORMED", "occurrenceId": identifier})
            target_ids = []
        if not isinstance(diagnostic_ids, list) or any(not isinstance(value, str) or not value for value in diagnostic_ids):
            failures.append({"code": "DIAGNOSTIC_IDS_MALFORMED", "occurrenceId": identifier})
            diagnostic_ids = []
        if len(set(target_ids)) != len(target_ids):
            failures.append({"code": "DUPLICATE_TARGET_ID", "occurrenceId": identifier})
        if len(set(diagnostic_ids)) != len(diagnostic_ids):
            failures.append({"code": "DUPLICATE_DIAGNOSTIC_ID", "occurrenceId": identifier})
        if rule is not None:
            allowed = set(rule.get("allowedDispositions", []))
            if disposition not in allowed:
                failures.append({"code": "DISPOSITION_POLICY_MISMATCH", "occurrenceId": identifier, "constructId": construct, "disposition": disposition, "allowed": sorted(allowed)})
            if rule.get("targetRequired") is True and disposition in COMPLETE_DISPOSITIONS and not target_ids:
                failures.append({"code": "TARGET_BINDING_REQUIRED", "occurrenceId": identifier})
            if disposition != "omitted-by-policy" and rule.get("diagnosticRequired") is True and disposition not in COMPLETE_DISPOSITIONS and not diagnostic_ids:
                failures.append({"code": "DIAGNOSTIC_BINDING_REQUIRED", "occurrenceId": identifier})
        if ir is not None:
            missing_targets = sorted(set(target_ids) - ir_targets)
            missing_diagnostics = sorted(set(diagnostic_ids) - ir_diagnostics)
            if missing_targets:
                failures.append({"code": "TARGET_NOT_IN_IR", "occurrenceId": identifier, "targetIds": missing_targets})
            if missing_diagnostics:
                failures.append({"code": "DIAGNOSTIC_NOT_IN_IR", "occurrenceId": identifier, "diagnosticIds": missing_diagnostics})
    for identifier in unknown_construct_ids:
        if identifier in account_by_id:
            account_by_id[identifier]["accountingState"] = "unknown-construct"
    for source in expected:
        identifier = source.get("sourceOccurrenceId")
        if not isinstance(identifier, str):
            continue
        if identifier in account_by_id and identifier not in unknown_construct_ids:
            account_by_id[identifier]["accountingState"] = "accounted"
    occurrence_rows: list[dict[str, Any]] = []
    for source in expected:
        identifier = source.get("sourceOccurrenceId")
        row = dict(source)
        account = account_by_id.get(identifier) if isinstance(identifier, str) else None
        if account is None:
            row.update({"disposition": None, "targetIds": [], "diagnosticIds": [], "accountingState": "unaccounted"})
        else:
            row.update({
                "disposition": account.get("disposition"),
                "targetIds": account.get("targetIds", []),
                "diagnosticIds": account.get("diagnosticIds", []),
                "accountingState": account.get("accountingState", "accounted"),
            })
        occurrence_rows.append(row)
    aggregate = aggregate_status(occurrence_rows, failures, unknown_construct_ids)
    checks = {
        "sourceOccurrenceIdsUnique": len(expected_by_id) == len(expected),
        "accountingOccurrenceIdsUnique": not duplicates,
        "unknownAccountingIdsZero": not unknown_ids,
        "unaccountedOccurrencesZero": not unaccounted_ids,
        "unknownConstructsZero": not unknown_construct_ids,
        "allAccountingFieldsValid": not any(item["code"].startswith(("ACCOUNTING_", "SOURCE_LOCATOR", "INVALID_DISPOSITION", "TARGET_IDS", "DIAGNOSTIC_IDS")) for item in failures),
        "targetAndDiagnosticBindingsValid": not any(item["code"] in {"TARGET_NOT_IN_IR", "DIAGNOSTIC_NOT_IN_IR"} for item in failures),
    }
    return {
        "status": "passed" if not failures else "failed",
        "failures": failures,
        "checks": checks,
        "sourceOccurrenceCount": len(expected),
        "accountingEntryCount": len(accounting),
        "unaccountedOccurrenceIds": unaccounted_ids,
        "duplicateOccurrenceIds": duplicates,
        "unknownAccountingIds": unknown_ids,
        "unknownConstructOccurrenceIds": sorted(unknown_construct_ids),
        "dispositionCounts": dict(sorted(disposition_counts.items())),
        "occurrenceRows": occurrence_rows,
        "aggregate": aggregate,
    }


def aggregate_status(rows: Sequence[dict[str, Any]], failures: Sequence[dict[str, Any]], unknown_construct_ids: Sequence[str] = ()) -> dict[str, Any]:
    counts = Counter(str(row.get("disposition")) for row in rows if row.get("accountingState") == "accounted" and row.get("disposition") is not None)
    hard_codes = {"UNACCOUNTED_OCCURRENCE", "DUPLICATE_OCCURRENCE_ACCOUNTING", "UNKNOWN_OCCURRENCE_ID", "UNKNOWN_CONSTRUCT", "INVALID_DISPOSITION", "ACCOUNTING_SOURCE_IDENTITY_MISMATCH", "ACCOUNTING_FIELD_MISSING"}
    hard_failure = bool(unknown_construct_ids) or any(item.get("code") in hard_codes for item in failures)
    noneligible = sorted(
        row["sourceOccurrenceId"]
        for row in rows
        if row.get("accountingState") == "accounted" and row.get("disposition") not in COMPLETE_DISPOSITIONS
    )
    status = "failed" if hard_failure else "partial" if noneligible or failures else "complete"
    return {
        "status": status,
        "completeAllowed": status == "complete",
        "counts": dict(sorted(counts.items())),
        "nonCompleteEligibleOccurrenceIds": noneligible,
        "unknownConstructCount": len(unknown_construct_ids),
        "failureCount": len(failures),
    }


def _assertions(validation: dict[str, Any]) -> list[dict[str, Any]]:
    checks = validation.get("checks", {})
    return [
        {"assertionId": key, "expected": True, "actual": bool(value), "status": "passed" if value else "failed"}
        for key, value in sorted(checks.items())
    ]


def build_reports(enumeration: dict[str, Any], validation: dict[str, Any]) -> dict[str, dict[str, Any]]:
    profile = profile_for_format(str(enumeration["format"]))
    common = {
        "schema": "fdir/qualification-issue-91-report",
        "version": "1.0.0",
        "issueNumber": 91,
        "format": enumeration["format"],
        "formatProfileId": enumeration["formatProfileId"],
        "evidenceCaseId": enumeration["evidenceCaseId"],
        "sourceSha": enumeration["sourceSha"],
        "sourceOccurrenceCount": enumeration["sourceOccurrenceCount"],
        "enumerator": enumeration["enumerator"],
        "capabilityProfileBound": profile["occurrenceAccounting"].get("profileBound", True),
    }
    status = validation["status"]
    source_report = {
        **common,
        "reportKind": "source-occurrence-accounting",
        "status": status,
        "conversionStatus": validation["aggregate"]["status"],
        "sourceOccurrences": validation["occurrenceRows"],
        "accounting": {
            "entryCount": validation["accountingEntryCount"],
            "dispositionCounts": validation["dispositionCounts"],
            "targetBindingCount": sum(1 for row in validation["occurrenceRows"] if row.get("targetIds")),
            "diagnosticBindingCount": sum(1 for row in validation["occurrenceRows"] if row.get("diagnosticIds")),
        },
        "checks": validation["checks"],
        "failures": validation["failures"],
        "assertions": _assertions(validation),
    }
    rule_counts: Counter[str] = Counter(str(item.get("constructId")) for item in validation["occurrenceRows"])
    unknown_constructs = [item for item in validation["occurrenceRows"] if item.get("constructId") not in _rules_by_construct(profile)]
    coverage = {
        **common,
        "reportKind": "capability-profile-coverage",
        "status": status,
        "profileBound": profile["occurrenceAccounting"].get("profileBound", True),
        "profileBindingFailure": None if profile["occurrenceAccounting"].get("profileBound", True) else "CAPABILITY_PROFILE_NOT_CONSTRUCT_CLOSED",
        "profileRules": [
            {
                "policyRuleId": rule["id"],
                "constructId": rule["constructId"],
                "support": rule.get("support"),
                "allowedDispositions": rule.get("allowedDispositions", []),
                "observedCount": rule_counts.get(rule["constructId"], 0),
            }
            for rule in profile["occurrenceAccounting"]["rules"]
        ],
        "unknownConstructs": [
            {"sourceOccurrenceId": item.get("sourceOccurrenceId"), "constructId": item.get("constructId"), "rawName": item.get("rawName")}
            for item in unknown_constructs
        ],
        "assertions": [
            {"assertionId": "construct-level-profile-closed", "expected": 0, "actual": len(unknown_constructs), "status": "passed" if not unknown_constructs else "failed"},
            {"assertionId": "every-observed-construct-has-policy", "expected": validation["sourceOccurrenceCount"], "actual": validation["sourceOccurrenceCount"] - len(unknown_constructs), "status": "passed" if not unknown_constructs else "failed"},
        ],
    }
    aggregation = {
        **common,
        "reportKind": "status-aggregation",
        "status": status,
        "conversionStatus": validation["aggregate"]["status"],
        "completeAllowed": validation["aggregate"]["completeAllowed"],
        "dispositionCounts": validation["aggregate"]["counts"],
        "nonCompleteEligibleOccurrenceIds": validation["aggregate"]["nonCompleteEligibleOccurrenceIds"],
        "unknownConstructCount": validation["aggregate"]["unknownConstructCount"],
        "failureCount": validation["aggregate"]["failureCount"],
        "assertions": [
            {"assertionId": "unaccounted-forbids-complete", "expected": 0, "actual": len(validation["unaccountedOccurrenceIds"]), "status": "passed" if not validation["unaccountedOccurrenceIds"] else "failed"},
            {"assertionId": "unknown-construct-forbids-complete", "expected": 0, "actual": validation["aggregate"]["unknownConstructCount"], "status": "passed" if validation["aggregate"]["unknownConstructCount"] == 0 else "failed"},
            {"assertionId": "complete-only-for-eligible-dispositions", "expected": validation["aggregate"]["completeAllowed"], "actual": validation["aggregate"]["status"] == "complete", "status": "passed" if validation["aggregate"]["completeAllowed"] == (validation["aggregate"]["status"] == "complete") else "failed"},
        ],
    }
    unaccounted = {
        **common,
        "reportKind": "unaccounted-occurrences",
        "status": status,
        "unaccountedOccurrenceIds": validation["unaccountedOccurrenceIds"],
        "duplicateOccurrenceIds": validation["duplicateOccurrenceIds"],
        "unknownAccountingIds": validation["unknownAccountingIds"],
        "unknownConstructOccurrenceIds": validation["unknownConstructOccurrenceIds"],
        "failures": validation["failures"],
        "assertions": [
            {"assertionId": "unaccounted-count-zero", "expected": 0, "actual": len(validation["unaccountedOccurrenceIds"]), "status": "passed" if not validation["unaccountedOccurrenceIds"] else "failed"},
            {"assertionId": "duplicate-count-zero", "expected": 0, "actual": len(validation["duplicateOccurrenceIds"]), "status": "passed" if not validation["duplicateOccurrenceIds"] else "failed"},
            {"assertionId": "unknown-accounting-id-count-zero", "expected": 0, "actual": len(validation["unknownAccountingIds"]), "status": "passed" if not validation["unknownAccountingIds"] else "failed"},
        ],
    }
    return {name: report for name, report in zip(REPORT_NAMES, (source_report, coverage, aggregation, unaccounted))}


def write_reports(reports: dict[str, dict[str, Any]], out_dir: Path) -> None:
    Path(out_dir).mkdir(parents=True, exist_ok=True)
    for name in REPORT_NAMES:
        if name not in reports:
            raise OccurrenceQualificationError(f"report is missing: {name}")
        Path(out_dir, name).write_text(_canonical(reports[name]) + "\n", encoding="utf-8", newline="\n")


def qualify_case(
    path: Path,
    format_name: str,
    *,
    accounting_path: Path | None = None,
    ir_path: Path | None = None,
    evidence_case_id: str | None = None,
    out_dir: Path | None = None,
    require_ir: bool = False,
) -> tuple[dict[str, Any], dict[str, dict[str, Any]]]:
    enumeration = enumerate_source(path, format_name, evidence_case_id=evidence_case_id)
    accounting = _load_accounting(accounting_path)
    ir = _read_json(ir_path) if ir_path is not None else None
    validation = validate_accounting(enumeration, accounting, ir=ir, require_ir=require_ir)
    reports = build_reports(enumeration, validation)
    if out_dir is not None:
        write_reports(reports, out_dir)
    return validation, reports


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="operation", required=True)
    enumerate_parser = sub.add_parser("enumerate")
    enumerate_parser.add_argument("input", type=Path)
    enumerate_parser.add_argument("--format", required=True, choices=("docx", "xlsx", "pdf", "markdown"))
    enumerate_parser.add_argument("--case-id")
    enumerate_parser.add_argument("--out", type=Path, required=True)
    qualify_parser = sub.add_parser("qualify")
    qualify_parser.add_argument("input", type=Path)
    qualify_parser.add_argument("--format", required=True, choices=("docx", "xlsx", "pdf", "markdown"))
    qualify_parser.add_argument("--case-id")
    qualify_parser.add_argument("--accounting", type=Path)
    qualify_parser.add_argument("--ir", type=Path)
    qualify_parser.add_argument("--require-ir", action="store_true")
    qualify_parser.add_argument("--out-dir", type=Path, required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    try:
        if args.operation == "enumerate":
            result = enumerate_source(args.input, args.format, evidence_case_id=args.case_id)
            args.out.parent.mkdir(parents=True, exist_ok=True)
            args.out.write_text(_canonical(result) + "\n", encoding="utf-8", newline="\n")
            print(_canonical({"status": "passed", "sourceOccurrenceCount": result["sourceOccurrenceCount"], "sourceSha": result["sourceSha"]}))
            return 0
        validation, reports = qualify_case(
            args.input,
            args.format,
            accounting_path=args.accounting,
            ir_path=args.ir,
            evidence_case_id=args.case_id,
            out_dir=args.out_dir,
            require_ir=args.require_ir,
        )
        print(_canonical({"status": validation["status"], "conversionStatus": validation["aggregate"]["status"], "sourceOccurrenceCount": validation["sourceOccurrenceCount"], "reports": sorted(reports)}))
        return 0 if validation["status"] == "passed" else 1
    except (OccurrenceQualificationError, OSError, UnicodeError, json.JSONDecodeError) as exc:
        print(f"FAIL: occurrence qualification: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
