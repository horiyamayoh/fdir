"""Shared bounded runtime helpers for the FDIR format adapters.

The adapter modules deliberately produce ordinary dictionaries so that the
wire format stays the JSON Schema in ``schemas/document-form-ir.schema.json``.
This module owns only assembly, deterministic identifiers, limits, and
diagnostic/report conventions; it does not interpret document meaning and it
never stores source bytes in an IR document.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
import hashlib
import io
import posixpath
import re
from pathlib import Path
from typing import Any, Iterable
import xml.etree.ElementTree as ET
import zipfile


SCHEMA = {"name": "fdir/document-form", "version": "1.0.0"}
STATUS_VALUES = {
    "preserved",
    "normalized",
    "approximated",
    "ambiguous",
    "unsupported",
    "omitted-by-policy",
    "unavailable",
    "failed",
}
DIAGNOSTIC_ACTIONS = {"review", "retry", "omit", "configure", "none"}


class AdapterError(Exception):
    """An input or resource-limit error that should become a failed IR."""


@dataclass(frozen=True)
class AdapterLimits:
    """Hard limits applied before and during conversion.

    The limits are intentionally conservative and format-independent.  A
    caller can use a smaller profile for untrusted input.  Timeout/cancellation
    belongs to the process boundary and is therefore represented by the
    caller, not simulated inside a parser.
    """

    max_input_bytes: int = 16 * 1024 * 1024
    max_nodes: int = 50_000
    max_text_chars: int = 2_000_000
    max_xml_parts: int = 500
    max_pdf_objects: int = 100_000
    max_zip_entries: int = 500
    max_zip_uncompressed_bytes: int = 64 * 1024 * 1024
    max_zip_entry_bytes: int = 16 * 1024 * 1024
    max_xml_bytes: int = 16 * 1024 * 1024
    max_xml_nodes: int = 100_000
    max_xml_depth: int = 256


_BOUNDED_LIMIT_FIELDS = (
    "max_input_bytes",
    "max_nodes",
    "max_text_chars",
    "max_xml_parts",
    "max_pdf_objects",
    "max_zip_entries",
    "max_zip_uncompressed_bytes",
    "max_zip_entry_bytes",
    "max_xml_bytes",
    "max_xml_nodes",
    "max_xml_depth",
)


def _validate_limits(limits: AdapterLimits) -> AdapterLimits:
    for field in _BOUNDED_LIMIT_FIELDS:
        value = getattr(limits, field)
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise AdapterError(f"{field} must be a non-negative integer")
    return limits


def _zip_member_name(name: str) -> str:
    """Validate and return the package-relative ZIP member name."""

    if not name or "\x00" in name:
        raise AdapterError("ZIP member name is empty or contains NUL")
    if "\\" in name:
        raise AdapterError(f"ZIP member uses a non-canonical path separator: {name!r}")
    if name.startswith("/") or re.match(r"^[A-Za-z]:", name):
        raise AdapterError(f"ZIP member path is absolute: {name!r}")
    directory = name.endswith("/")
    canonical_name = name[:-1] if directory else name
    if not canonical_name:
        raise AdapterError(f"ZIP member path is empty: {name!r}")
    components = canonical_name.split("/")
    if any(component in {"", ".", ".."} for component in components):
        raise AdapterError(f"ZIP member path is not package-relative: {name!r}")
    if posixpath.normpath(canonical_name) != canonical_name:
        raise AdapterError(f"ZIP member path is not canonical: {name!r}")
    return name


def validate_zip_archive(archive: zipfile.ZipFile, limits: AdapterLimits) -> list[str]:
    """Fail closed on ZIP structure and declared expansion size."""

    _validate_limits(limits)
    infos = archive.infolist()
    if len(infos) > limits.max_zip_entries:
        raise AdapterError(f"ZIP entry limit exceeded: {len(infos)} > {limits.max_zip_entries}")
    names: list[str] = []
    seen: set[str] = set()
    total_uncompressed = 0
    for info in infos:
        name = _zip_member_name(info.filename)
        duplicate_key = name.rstrip("/")
        if duplicate_key in seen:
            raise AdapterError(f"duplicate ZIP member name: {name}")
        seen.add(duplicate_key)
        declared_size = int(info.file_size)
        if declared_size < 0:
            raise AdapterError(f"negative ZIP member size: {name}")
        if declared_size > limits.max_zip_entry_bytes:
            raise AdapterError(
                f"ZIP member size limit exceeded for {name}: "
                f"{declared_size} > {limits.max_zip_entry_bytes}"
            )
        total_uncompressed += declared_size
        if total_uncompressed > limits.max_zip_uncompressed_bytes:
            raise AdapterError(
                "ZIP uncompressed-size limit exceeded: "
                f"{total_uncompressed} > {limits.max_zip_uncompressed_bytes}"
            )
        names.append(name)
    return names


def read_bounded_zip_member(archive: zipfile.ZipFile, name: str, limits: AdapterLimits, *, xml: bool = False) -> bytes:
    """Read one ZIP member without allowing decompression beyond its budget."""

    _validate_limits(limits)
    normalized_name = _zip_member_name(name)
    try:
        info = archive.getinfo(normalized_name)
    except KeyError as exc:
        raise AdapterError(f"ZIP member is missing: {normalized_name}") from exc
    max_bytes = limits.max_xml_bytes if xml else limits.max_zip_entry_bytes
    if info.file_size > max_bytes:
        kind = "XML" if xml else "ZIP member"
        raise AdapterError(f"{kind} size limit exceeded for {normalized_name}: {info.file_size} > {max_bytes}")
    try:
        with archive.open(info, "r") as stream:
            payload = stream.read(max_bytes + 1)
    except (RuntimeError, EOFError, OSError, zipfile.BadZipFile) as exc:
        raise AdapterError(f"unable to read ZIP member {normalized_name}: {exc}") from exc
    if len(payload) > max_bytes:
        kind = "XML" if xml else "ZIP member"
        raise AdapterError(f"{kind} read budget exceeded for {normalized_name}: > {max_bytes}")
    return payload


def read_bounded_xml(archive: zipfile.ZipFile, name: str, limits: AdapterLimits) -> ET.Element:
    """Parse XML only after applying byte, entity, node, depth, and text caps."""

    payload = read_bounded_zip_member(archive, name, limits, xml=True)
    upper_payload = payload.upper()
    if b"<!DOCTYPE" in upper_payload or b"<!ENTITY" in upper_payload:
        raise AdapterError(f"XML DTD/entity declarations are not supported: {name}")
    node_count = 0
    text_chars = 0
    depth = 0
    root: ET.Element | None = None
    try:
        # Iterparse enforces depth/node budgets while the XML is being built,
        # instead of first materializing an unchecked tree with fromstring().
        parser = ET.iterparse(io.BytesIO(payload), events=("start", "end"))
        for event, element in parser:
            if event == "start":
                depth += 1
                if depth > limits.max_xml_depth:
                    raise AdapterError(f"XML depth limit exceeded: {depth} > {limits.max_xml_depth}")
                node_count += 1
                if node_count > limits.max_xml_nodes:
                    raise AdapterError(f"XML node limit exceeded: {node_count} > {limits.max_xml_nodes}")
                if root is None:
                    root = element
                text_chars += sum(len(key) + len(value) for key, value in element.attrib.items())
            else:
                text_chars += len(element.text or "") + len(element.tail or "")
                if text_chars > limits.max_text_chars:
                    raise AdapterError(f"XML text budget exceeded: {text_chars} > {limits.max_text_chars}")
                depth -= 1
    except AdapterError:
        raise
    except ET.ParseError as exc:
        raise AdapterError(f"XML parse failed for {name}: {exc}") from exc
    if root is None:
        raise AdapterError(f"XML document is empty: {name}")
    return root


def safe_id(prefix: str, value: str, *, limit: int = 96) -> str:
    """Return a deterministic schema-compatible identifier."""

    raw = re.sub(r"[^A-Za-z0-9_.:/-]+", "-", str(value)).strip("-")
    if not raw:
        raw = "item"
    if not re.match(r"^[A-Za-z]", raw):
        raw = "x-" + raw
    candidate = f"{prefix}-{raw}"
    if len(candidate) <= limit:
        return candidate
    digest = hashlib.sha256(candidate.encode("utf-8")).hexdigest()[:16]
    return candidate[: limit - 17].rstrip("-") + "-" + digest


def file_document_id(path: Path, format_name: str) -> str:
    """Create a stable document id without placing file bytes in the IR."""

    resolved = path.resolve()
    token = f"{format_name}:{resolved.name}"
    return safe_id("doc", token)


def document_content_id(document: dict[str, Any]) -> str:
    """Return a path-independent identity for source-declared form facts.

    Ingestion path, source maps, diagnostics, conversion status, observations,
    and the id itself are deliberately outside this projection.  The source
    bytes are never copied into the IR or into this identity input.
    """

    # Keep adapter identity exactly aligned with the public canonicalizer.
    # Maintaining a second normalizer here allowed collection/order rules to
    # drift while both implementations still appeared deterministic.
    try:
        from canonicalize_ir import canonical_digest  # type: ignore
    except ImportError:  # pragma: no cover - package-style import
        from tools.canonicalize_ir import canonical_digest  # type: ignore
    try:
        digest = canonical_digest(document, "source-map-excluded")
    except Exception as exc:  # pragma: no cover - converted to adapter failure by caller
        raise AdapterError(f"cannot derive canonical document identity: {exc}") from exc
    return safe_id("doc", digest)


def text_value(value: Any, *, max_chars: int) -> str:
    text = "" if value is None else str(value)
    if len(text) > max_chars:
        raise AdapterError(f"text limit exceeded: {len(text)} > {max_chars}")
    return text


def decimal(value: Any) -> str:
    """Return a canonical fixed-point decimal without lossy fallback.

    Exponents are accepted as input and normalized to fixed point.  Invalid,
    non-finite, and NaN values raise ``AdapterError`` so an adapter cannot
    fabricate zero while claiming a preserved source value.
    """

    if isinstance(value, bool):
        value = int(value)
    if isinstance(value, float) and (value != value or value in (float("inf"), float("-inf"))):
        raise AdapterError(f"non-finite decimal value: {value!r}")
    token = str(value).strip()
    if not token:
        raise AdapterError("empty decimal value")
    if not re.fullmatch(r"[+-]?(?:(?:\d+(?:\.\d*)?)|(?:\.\d+))(?:[eE][+-]?\d+)?", token):
        raise AdapterError(f"invalid decimal value: {token!r}")
    try:
        parsed = Decimal(token)
    except InvalidOperation as exc:
        raise AdapterError(f"invalid decimal value: {token!r}") from exc
    if not parsed.is_finite():
        raise AdapterError(f"non-finite decimal value: {token!r}")
    if parsed == 0:
        return "0"
    fixed = format(parsed, "f")
    if fixed.startswith("+"):
        fixed = fixed[1:]
    if fixed.startswith("-0.") and Decimal(fixed) == 0:
        return "0"
    if "." in fixed:
        fixed = fixed.rstrip("0").rstrip(".")
    if fixed in {"-0", "+0", ""}:
        return "0"
    return fixed


class DocumentBuilder:
    """Small deterministic assembler shared by all input adapters."""

    def __init__(
        self,
        path: Path,
        format_name: str,
        format_version: str,
        *,
        limits: AdapterLimits | None = None,
        root_kind: str = "document",
    ) -> None:
        self.path = Path(path)
        self.format_name = format_name
        self.format_version = format_version
        self.limits = limits or AdapterLimits()
        self.diagnostics: list[dict[str, Any]] = []
        self.features: list[dict[str, Any]] = []
        self._ids: set[str] = set()
        self._node_count = 0
        self._text_chars = 0
        root_id = safe_id("node", f"{format_name}-document")
        self.root_id = root_id
        self.document: dict[str, Any] = {
            "schema": dict(SCHEMA),
            "documentId": file_document_id(self.path, format_name),
            "sourceFormat": {
                "namespace": "format",
                "name": format_name,
                "version": format_version,
            },
            "rootNodeId": root_id,
            "parts": [],
            "surfaces": [],
            "nodes": [],
            "texts": [],
            "tables": [],
            "styles": [],
            "layouts": [],
            "coordinateSpaces": [],
            "geometries": [],
            "resources": [],
            "formulas": [],
            "fields": [],
            "annotations": [],
            "relations": [],
            "orders": [],
            "observations": [],
            "extensions": [],
            "sourceMaps": [],
            "diagnostics": self.diagnostics,
            "conversion": {
                "status": "complete",
                "capabilityProfile": f"format:{format_name}:{format_version}:1",
                "features": self.features,
                "featureInventory": [],
                "warnings": [],
                "diagnostics": [],
            },
        }
        self.add_node(root_kind, root_id, status="preserved")

    def _reserve(self, item_id: str) -> str:
        if item_id in self._ids:
            raise AdapterError(f"duplicate generated id: {item_id}")
        self._ids.add(item_id)
        return item_id

    def _check_node_limit(self) -> None:
        if self._node_count >= self.limits.max_nodes:
            raise AdapterError(f"node limit exceeded: {self.limits.max_nodes}")

    def add_node(
        self,
        kind: str,
        node_id: str,
        *,
        parent_id: str | None = None,
        status: str = "preserved",
        **fields: Any,
    ) -> dict[str, Any]:
        self._check_node_limit()
        if status not in STATUS_VALUES:
            raise AdapterError(f"invalid node status: {status}")
        node = {
            "nodeId": self._reserve(node_id),
            "kind": kind,
            "childIds": [],
            "status": status,
        }
        if parent_id:
            node["parentId"] = parent_id
            parent = self.find("nodes", "nodeId", parent_id)
            if parent is None:
                raise AdapterError(f"unknown parent node: {parent_id}")
            parent.setdefault("childIds", []).append(node_id)
        aliases = {
            "part_id": "partId",
            "text_ids": "textIds",
            "style_ids": "styleIds",
            "direct_style_id": "directStyleId",
            "resolved_style_id": "resolvedStyleId",
            "geometry_id": "geometryId",
            "formula_id": "formulaId",
            "field_id": "fieldId",
            "annotation_ids": "annotationIds",
            "resource_ids": "resourceIds",
            "layout_ids": "layoutIds",
        }
        node.update({aliases.get(key, key): value for key, value in fields.items() if value is not None})
        if kind == "run":
            node.setdefault("textIds", [])
        self.document["nodes"].append(node)
        self._node_count += 1
        return node

    def add_text(
        self,
        text_id: str,
        value: Any,
        *,
        representation: str = "source",
        provenance: str = "authored",
        source_text_id: str | None = None,
        source_range: dict[str, int] | None = None,
        transformations: Iterable[dict[str, Any]] = (),
        status: str = "preserved",
    ) -> dict[str, Any]:
        text = text_value(value, max_chars=self.limits.max_text_chars - self._text_chars)
        self._text_chars += len(text)
        if status not in STATUS_VALUES:
            raise AdapterError(f"invalid text status: {status}")
        item = {
            "textId": self._reserve(text_id),
            "representation": representation,
            "provenance": provenance,
            "value": text,
            "status": status,
        }
        if source_text_id:
            item["sourceTextId"] = source_text_id
        if source_range is not None:
            item["sourceRange"] = dict(source_range)
        transformation_list = list(transformations)
        if transformation_list:
            item["transformations"] = transformation_list
        self.document["texts"].append(item)
        return item

    def add_item(self, collection: str, item: dict[str, Any], item_id_key: str | None = None) -> dict[str, Any]:
        item = {key: value for key, value in item.items() if value is not None}
        if item_id_key:
            self._reserve(str(item[item_id_key]))
        self.document.setdefault(collection, []).append(item)
        return item

    def find(self, collection: str, key: str, value: Any) -> dict[str, Any] | None:
        return next((item for item in self.document.get(collection, []) if item.get(key) == value), None)

    def link_text(self, node_id: str, text_id: str) -> None:
        node = self.find("nodes", "nodeId", node_id)
        if node is None:
            raise AdapterError(f"unknown node: {node_id}")
        node.setdefault("textIds", []).append(text_id)

    def add_source_map(self, target_id: str, locator: dict[str, Any]) -> dict[str, Any]:
        map_id = safe_id("map", f"{target_id}:{len(self.document['sourceMaps'])}")
        item = {
            "sourceMapId": self._reserve(map_id),
            "targetId": target_id,
            "format": dict(self.document["sourceFormat"]),
            "locator": {"kind": self.format_name, **locator},
        }
        self.document["sourceMaps"].append(item)
        return item

    def add_feature(self, feature: str, status: str = "preserved", *, target_id: str | None = None, diagnostic_ids: Iterable[str] = ()) -> None:
        item: dict[str, Any] = {"feature": feature, "status": status}
        if target_id:
            item["targetId"] = target_id
        ids = list(diagnostic_ids)
        if ids:
            item["diagnosticIds"] = ids
        self.features.append(item)

    def add_diagnostic(
        self,
        code: str,
        message: str,
        *,
        severity: str = "warning",
        phase: str = "parse",
        target_id: str | None = None,
        related_ids: Iterable[str] = (),
        action: str = "review",
    ) -> str:
        if action not in DIAGNOSTIC_ACTIONS:
            raise AdapterError(f"invalid diagnostic action: {action}")
        diagnostic_id = safe_id("diagnostic", f"{self.format_name}-{len(self.diagnostics)}-{code}")
        item: dict[str, Any] = {
            "diagnosticId": self._reserve(diagnostic_id),
            "code": code,
            "severity": severity,
            "phase": phase,
            "message": message,
            "action": action,
        }
        if target_id:
            item["targetId"] = target_id
        related = list(related_ids)
        if related:
            item["relatedIds"] = related
        self.diagnostics.append(item)
        return diagnostic_id

    def finish(self, *, status: str | None = None) -> dict[str, Any]:
        if status is None:
            source_loss = any(
                item.get("status") in {"approximated", "ambiguous", "unsupported", "omitted-by-policy", "failed"}
                for collection in ("parts", "surfaces", "nodes", "texts", "tables", "styles", "layouts", "geometries", "resources", "formulas", "fields", "annotations", "relations", "orders", "extensions")
                for item in self.document.get(collection, [])
                if isinstance(item, dict)
            ) or any(item.get("status") in {"approximated", "ambiguous", "unsupported", "omitted-by-policy", "failed"} for item in self.features)
            has_error = any(d.get("severity") in {"fatal", "error"} for d in self.diagnostics)
            has_warning = any(d.get("severity") in {"info", "warning"} for d in self.diagnostics)
            status = "failed" if has_error else "partial" if source_loss else "complete-with-warnings" if has_warning else "complete"
        if status not in {"complete", "complete-with-warnings", "partial", "failed"}:
            raise AdapterError(f"invalid conversion status: {status}")
        inventory: dict[tuple[str, str], dict[str, Any]] = {}
        for feature in self.features:
            key = (str(feature.get("feature")), str(feature.get("status")))
            entry = inventory.setdefault(key, {"feature": key[0], "occurrences": 0, "disposition": "core", "status": key[1]})
            entry["occurrences"] += 1
            diagnostic_ids = feature.get("diagnosticIds", [])
            if diagnostic_ids:
                entry.setdefault("diagnosticIds", []).extend(item for item in diagnostic_ids if item not in entry.setdefault("diagnosticIds", []))
            if key[1] in {"unsupported", "omitted-by-policy", "failed", "ambiguous", "approximated"}:
                entry["disposition"] = "non-preserved"
            elif key[1] == "unavailable":
                entry["disposition"] = "observation"
            elif key[0] in {"renderer-observation", "ocr-observation"}:
                entry["disposition"] = "observation"
            elif key[0].endswith("-extension") or key[0] == "extension":
                entry["disposition"] = "extension"
        self.document["conversion"]["featureInventory"] = sorted(inventory.values(), key=lambda item: (item["feature"], item["status"]))
        self.document["conversion"]["status"] = status
        self.document["conversion"]["warnings"] = [d["diagnosticId"] for d in self.diagnostics if d.get("severity") in {"info", "warning"}]
        self.document["conversion"]["diagnostics"] = [d["diagnosticId"] for d in self.diagnostics]
        self.document["documentId"] = document_content_id(self.document)
        return self.document


def input_limit_check(path: Path, limits: AdapterLimits | None = None) -> AdapterLimits:
    limits = limits or AdapterLimits()
    _validate_limits(limits)
    if not path.is_file():
        raise AdapterError(f"input is not a regular file: {path}")
    size = path.stat().st_size
    if size > limits.max_input_bytes:
        raise AdapterError(f"input size limit exceeded: {size} > {limits.max_input_bytes}")
    return limits


def failed_document(path: Path, format_name: str, format_version: str, code: str, message: str) -> dict[str, Any]:
    """Return a schema-shaped failed result for malformed/unavailable input."""

    builder = DocumentBuilder(path, format_name, format_version)
    diagnostic_id = builder.add_diagnostic(code, message, severity="error", phase="parse")
    builder.add_feature("input", "failed", diagnostic_ids=[diagnostic_id])
    return builder.finish(status="failed")
