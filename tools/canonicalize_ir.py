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
from decimal import Decimal
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


CANONICALIZATION_PATH = Path(__file__).resolve().parents[1] / "machine" / "canonicalization.json"


def _utf16_sort_key(value: str) -> tuple[int, ...]:
    """Return the JSON contract's UTF-16 code-unit lexical sort key."""

    encoded = value.encode("utf-16-be", "surrogatepass")
    return tuple(int.from_bytes(encoded[offset : offset + 2], "big") for offset in range(0, len(encoded), 2))


def _unique_object_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    """Reject duplicate JSON object members before Python collapses them."""

    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON object key: {key}")
        result[key] = value
    return result


def _reject_json_constant(token: str) -> Any:
    raise ValueError(f"non-JSON numeric constant: {token}")


def _canonicalization_config() -> dict[str, Any]:
    try:
        value = json.loads(CANONICALIZATION_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise CanonicalizationError(f"cannot load canonicalization registry: {exc}") from exc
    if not isinstance(value, dict) or not isinstance(value.get("entityCollections"), dict):
        raise CanonicalizationError("canonicalization registry lacks entityCollections")
    return value


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
    elif isinstance(value, float):
        if value != value or value in (float("inf"), float("-inf")):
            raise CanonicalizationError(f"non-finite number at {path}")
        # Exact form values are strings in the IR.  Rejecting JSON floating
        # point values prevents Python's exponent spelling and binary float
        # rounding from becoming part of an authoritative digest.  JSON
        # integers remain valid for schema fields such as ordinals and zIndex.
        raise CanonicalizationError(f"floating-point JSON number must be an exact decimal string at {path}")
    elif isinstance(value, Decimal):
        raise CanonicalizationError(f"Decimal objects are not JSON IR values at {path}")


def _validate_authority(document: dict[str, Any]) -> None:
    try:
        from ir_validation import validate_document  # type: ignore
    except ImportError:  # pragma: no cover - package-style import
        from tools.ir_validation import validate_document  # type: ignore
    validate_document(document)


def _canonical_bytes(value: Any, *, validate: bool) -> bytes:
    if validate:
        if not isinstance(value, dict):
            raise CanonicalizationError("IR document must be a JSON object")
        _validate_authority(value)
    _walk(value)
    normalized = _normalize(value)
    try:
        encoded = json.dumps(
            normalized,
            ensure_ascii=False,
            sort_keys=False,
            separators=(",", ":"),
            allow_nan=False,
        )
        return encoded.encode("utf-8")
    except (TypeError, ValueError, UnicodeError) as exc:
        raise CanonicalizationError(f"document is not canonicalizable: {exc}") from exc


def canonical_bytes(document: dict[str, Any], projection: str = "full") -> bytes:
    """Return deterministic UTF-8 JSON bytes for a named IR projection."""

    if not isinstance(document, dict):
        raise CanonicalizationError("IR document must be a JSON object")
    _validate_authority(document)
    projected = projection_document(document, projection)
    return _canonical_bytes(projected, validate=False)


def canonical_value_bytes(value: Any) -> bytes:
    """Canonicalize a JSON value using the same ordering and number rules.

    This is used by the rebuildable query index for per-entity fact digests;
    it does not make an entity fragment authoritative on its own.
    """

    return _canonical_bytes(value, validate=False)


def canonical_value_digest(value: Any) -> str:
    return hashlib.sha256(canonical_value_bytes(value)).hexdigest()


_ID_ARRAYS = {str(key): str(value) for key, value in _canonicalization_config()["entityCollections"].items()}


def _normalize(value: Any, field: str | None = None) -> Any:
    """Normalize only arrays whose entries expose an explicit stable identity.

    Child/reference arrays retain their source order because that order is a
    document-form fact. Entity collections are sorted by their IDs so a storage
    layer cannot change IR identity by returning rows in a different order.
    """

    if isinstance(value, dict):
        normalized = ((key, _normalize(child, key)) for key, child in value.items())
        return {key: child for key, child in sorted(normalized, key=lambda item: _utf16_sort_key(item[0]))}
    if isinstance(value, list):
        result = [_normalize(child) for child in value]
        id_field = _ID_ARRAYS.get(field or "")
        if id_field and all(isinstance(item, dict) and isinstance(item.get(id_field), str) for item in result):
            return sorted(result, key=lambda item: item[id_field])
        if field == "items" and all(isinstance(item, dict) and isinstance(item.get("ordinal"), int) and not isinstance(item.get("ordinal"), bool) for item in result):
            return sorted(result, key=lambda item: item["ordinal"])
        return result
    return value


def projection_document(document: dict[str, Any], projection: str = "source-map-excluded") -> dict[str, Any]:
    """Return a named identity projection without mutating the source object."""

    if projection not in {"full", "content", "source-map-excluded"}:
        raise CanonicalizationError(f"unknown digest projection: {projection}")
    projected = copy.deepcopy(document)
    if projection == "full":
        return projected
    # Source maps, diagnostics, conversion outcomes, observations, and the
    # caller-visible documentId are not source-declared form content.
    for key in ("documentId", "sourceMaps", "diagnostics", "conversion", "observations"):
        projected.pop(key, None)
    return projected


def canonical_digest(document: dict[str, Any], projection: str = "source-map-excluded") -> str:
    """Return a SHA-256 digest for the named, validated IR projection."""

    return hashlib.sha256(canonical_bytes(document, projection)).hexdigest()


def full_canonical_digest(document: dict[str, Any]) -> str:
    """Return the digest of every canonical IR field, including locators."""

    return canonical_digest(document, "full")


def migrate_document(document: dict[str, Any], target_version: str) -> tuple[dict[str, Any], list[dict[str, str]]]:
    """Perform the intentionally narrow version migration boundary.

    Version ``1.0.0`` is currently the only supported wire version.  The
    function still returns a receipt-shaped diagnostic list so future schema
    migrations cannot silently discard fields or pretend compatibility.
    """

    _validate_authority(document)
    if target_version != document.get("schema", {}).get("version"):
        raise CanonicalizationError(f"no registered migration to {target_version}")
    return copy.deepcopy(document), [{
        "ruleId": "FDIR-MIGRATE-1.0.0-NOOP",
        "sourceVersion": str(document.get("schema", {}).get("version")),
        "targetVersion": target_version,
        "status": "preserved",
        "loss": "none",
    }]


def load_document(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(
            path.read_text(encoding="utf-8"),
            object_pairs_hook=_unique_object_pairs,
            parse_constant=_reject_json_constant,
        )
    except FileNotFoundError as exc:
        raise CanonicalizationError(f"missing input: {path}") from exc
    except (json.JSONDecodeError, ValueError, UnicodeError) as exc:
        raise CanonicalizationError(f"invalid JSON in {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise CanonicalizationError("IR document must be a JSON object")
    return value


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input", type=Path, help="Document Form IR JSON file")
    parser.add_argument("--digest", action="store_true", help="print only the SHA-256 digest")
    parser.add_argument("--projection", choices=("full", "content", "source-map-excluded"), default="source-map-excluded", help="digest identity projection")
    parser.add_argument("--output", type=Path, help="write canonical JSON to this file")
    parser.add_argument("--check", action="store_true", help="fail unless input bytes are already canonical")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        document = load_document(args.input)
        encoded = canonical_bytes(document, args.projection)
        canonical_file = encoded + b"\n"
        if args.check and args.projection != "full":
            raise CanonicalizationError("--check is only defined for the full document projection")
        if args.check and args.input.read_bytes() not in {encoded, canonical_file}:
            raise CanonicalizationError("input is not in canonical JSON form")
        if args.output:
            args.output.write_bytes(canonical_file)
        elif args.digest:
            print(hashlib.sha256(encoded).hexdigest())
        else:
            sys.stdout.buffer.write(canonical_file)
    except (OSError, CanonicalizationError) as exc:
        print(f"CANONICALIZATION ERROR: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
