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
import json
import re
from pathlib import Path
from typing import Any, Iterable


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


_ENTITY_ID_FIELDS = {
    "parts": "partId", "surfaces": "surfaceId", "nodes": "nodeId", "texts": "textId",
    "tables": "tableId", "styles": "styleId", "layouts": "layoutId", "coordinateSpaces": "coordinateSpaceId",
    "geometries": "geometryId", "resources": "resourceId", "formulas": "formulaId", "fields": "fieldId",
    "annotations": "annotationId", "relations": "relationId", "orders": "orderId", "extensions": "extensionId",
}


def _identity_normalize(value: Any, field: str | None = None) -> Any:
    if isinstance(value, dict):
        return {key: _identity_normalize(child, key) for key, child in sorted(value.items())}
    if isinstance(value, list):
        values = [_identity_normalize(child) for child in value]
        identifier = _ENTITY_ID_FIELDS.get(field or "")
        if identifier and all(isinstance(item, dict) and isinstance(item.get(identifier), str) for item in values):
            return sorted(values, key=lambda item: item[identifier])
        return values
    return value


def document_content_id(document: dict[str, Any]) -> str:
    """Return a path-independent identity for source-declared form facts.

    Ingestion path, source maps, diagnostics, conversion status, observations,
    and the id itself are deliberately outside this projection.  The source
    bytes are never copied into the IR or into this identity input.
    """

    excluded = {"documentId", "sourceMaps", "diagnostics", "conversion", "observations"}
    projection = {key: value for key, value in document.items() if key not in excluded}
    encoded = json.dumps(_identity_normalize(projection), ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")
    return safe_id("doc", hashlib.sha256(encoded).hexdigest())


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
    ) -> str:
        diagnostic_id = safe_id("diagnostic", f"{self.format_name}-{len(self.diagnostics)}-{code}")
        item: dict[str, Any] = {
            "diagnosticId": self._reserve(diagnostic_id),
            "code": code,
            "severity": severity,
            "phase": phase,
            "message": message,
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
            status = "failed" if any(d.get("severity") in {"fatal", "error"} for d in self.diagnostics) else "partial" if source_loss else "complete"
        if status not in {"complete", "partial", "failed"}:
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
