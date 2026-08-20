"""Canonical Document Form IR serialization and identity.

This module intentionally has no parser or source-byte store.  It provides the
small, deterministic operation that the design exposes as the IR authority:
canonical JSON bytes and a digest over those bytes.
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import sys
from pathlib import Path
from typing import Any


FORBIDDEN_KEYS = {
    "sourceBytes",
    "sourceByteStore",
    "contentAddressedSource",
    "semanticEquivalence",
    "EquivalenceCertificate",
    "LineageCertificate",
    "AccountingItem",
    "predicate",
}


class CanonicalizationError(ValueError):
    """Raised when a value cannot be an authoritative IR document."""


def _walk(value: Any, path: str = "$") -> None:
    if isinstance(value, dict):
        for key, child in value.items():
            if key in FORBIDDEN_KEYS:
                raise CanonicalizationError(f"forbidden IR field at {path}: {key}")
            if not isinstance(key, str):
                raise CanonicalizationError(f"object key is not a string at {path}")
            _walk(child, f"{path}.{key}")
    elif isinstance(value, list):
        for index, child in enumerate(value):
            _walk(child, f"{path}[{index}]")
    elif isinstance(value, float) and (value != value or value in (float("inf"), float("-inf"))):
        raise CanonicalizationError(f"non-finite number at {path}")


def canonical_bytes(document: dict[str, Any]) -> bytes:
    """Return deterministic UTF-8 JSON bytes for an IR document."""

    if not isinstance(document, dict):
        raise CanonicalizationError("IR document must be a JSON object")
    _walk(document)
    normalized = _normalize(document)
    try:
        return json.dumps(
            normalized,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise CanonicalizationError(f"document is not canonicalizable: {exc}") from exc


_ID_ARRAYS = {
    "parts": "partId",
    "surfaces": "surfaceId",
    "nodes": "nodeId",
    "texts": "textId",
    "tables": "tableId",
    "styles": "styleId",
    "layouts": "layoutId",
    "coordinateSpaces": "coordinateSpaceId",
    "geometries": "geometryId",
    "resources": "resourceId",
    "formulas": "formulaId",
    "fields": "fieldId",
    "annotations": "annotationId",
    "relations": "relationId",
    "orders": "orderId",
    "observations": "observationId",
    "extensions": "extensionId",
    "sourceMaps": "sourceMapId",
    "diagnostics": "diagnosticId",
}


def _normalize(value: Any, field: str | None = None) -> Any:
    """Normalize only arrays whose entries expose an explicit stable identity.

    Child/reference arrays retain their source order because that order is a
    document-form fact. Entity collections are sorted by their IDs so a storage
    layer cannot change IR identity by returning rows in a different order.
    """

    if isinstance(value, dict):
        return {key: _normalize(child, key) for key, child in value.items()}
    if isinstance(value, list):
        result = [_normalize(child) for child in value]
        id_field = _ID_ARRAYS.get(field or "")
        if id_field and all(isinstance(item, dict) and isinstance(item.get(id_field), str) for item in result):
            return sorted(result, key=lambda item: item[id_field])
        if field == "items" and all(isinstance(item, dict) and isinstance(item.get("ordinal"), int) for item in result):
            return sorted(result, key=lambda item: item["ordinal"])
        return result
    return value


def canonical_digest(document: dict[str, Any]) -> str:
    """Return the SHA-256 identity of canonical IR bytes."""

    identity_document = copy.deepcopy(document)
    # Source maps are optional locators and explicitly outside IR identity.
    identity_document.pop("sourceMaps", None)
    return hashlib.sha256(canonical_bytes(identity_document)).hexdigest()


def load_document(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise CanonicalizationError(f"missing input: {path}") from exc
    except json.JSONDecodeError as exc:
        raise CanonicalizationError(f"invalid JSON in {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise CanonicalizationError("IR document must be a JSON object")
    return value


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input", type=Path, help="Document Form IR JSON file")
    parser.add_argument("--digest", action="store_true", help="print only the SHA-256 digest")
    parser.add_argument("--output", type=Path, help="write canonical JSON to this file")
    parser.add_argument("--check", action="store_true", help="fail unless input bytes are already canonical")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        document = load_document(args.input)
        encoded = canonical_bytes(document)
        if args.check and args.input.read_bytes() != encoded:
            raise CanonicalizationError("input is not in canonical JSON form")
        if args.output:
            args.output.write_bytes(encoded + b"\n")
        elif args.digest:
            print(hashlib.sha256(encoded).hexdigest())
        else:
            sys.stdout.buffer.write(encoded + b"\n")
    except (OSError, CanonicalizationError) as exc:
        print(f"CANONICALIZATION ERROR: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
