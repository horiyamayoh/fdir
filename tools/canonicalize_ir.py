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


def migrate_extensions(document: dict[str, Any], target_version: str) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Migrate extension schema versions with explicit loss receipts.

    Extension migration is separate from the document wire-version migration:
    an extension's ``schemaVersion`` is negotiated against the registry while
    the surrounding IR remains at its authoritative document version.  Every
    accepted, opaque-preserved, or loss-bearing decision returns a receipt;
    unsupported downgrades are retained with a failed receipt and are never
    silently relabelled.
    """

    if not isinstance(document, dict) or not isinstance(target_version, str):
        raise CanonicalizationError("extension migration requires a document object and target_version string")
    extensions = document.get("extensions")
    if not isinstance(extensions, list):
        raise CanonicalizationError("extension migration requires an extensions array")
    try:
        from extension_registry import load_registry  # type: ignore
    except ImportError:  # pragma: no cover - package-style import
        from tools.extension_registry import load_registry  # type: ignore
    registry = load_registry()
    entries = {
        (entry.get("namespace"), entry.get("type"), entry.get("schemaVersion")): entry
        for entry in registry.get("entries", [])
        if isinstance(entry, dict)
    }
    migrated = copy.deepcopy(document)
    receipts: list[dict[str, Any]] = []
    for extension in migrated["extensions"]:
        if not isinstance(extension, dict):
            raise CanonicalizationError("extension migration encountered a non-object extension")
        source_version = extension.get("schemaVersion")
        if not isinstance(source_version, str):
            raise CanonicalizationError("extension migration requires schemaVersion on every extension")
        key = (extension.get("namespace"), extension.get("type"), source_version)
        entry = entries.get(key)
        losses: list[str] = []
        dropped_fields: list[str] = []
        rule_id = "opaque-preserve"
        status = "opaque-preserved" if entry is None else "preserved"
        if source_version == target_version:
            if entry is not None:
                rule_id = "identity-preserve"
        elif source_version == "1.0.0" and target_version == "1.1.0":
            format_name = str(extension.get("namespace", "extension")).rsplit(":", 1)[-1]
            rule_id = f"{format_name}-{extension.get('type', 'extension')}-1.0.0-to-1.1.0"
            extension["schemaVersion"] = target_version
        elif source_version == "1.0.0" and target_version == "2.0.0":
            format_name = str(extension.get("namespace", "extension")).rsplit(":", 1)[-1]
            rule_id = f"{format_name}-{extension.get('type', 'extension')}-1.0.0-to-2.0.0"
            payload = extension.get("payload")
            if isinstance(payload, dict) and "legacyRange" in payload:
                payload.pop("legacyRange", None)
                losses.append("/payload/legacyRange")
                dropped_fields.append("legacyRange")
            extension["schemaVersion"] = target_version
            status = "loss-declared" if losses else "preserved"
        elif source_version == "2.0.0" and target_version == "1.0.0":
            rule_id = "none"
            losses.append("unsupported-downgrade")
            status = "failed"
        else:
            raise CanonicalizationError(f"no registered extension migration from {source_version} to {target_version}")
        receipts.append({
            "receiptId": f"extension-migration-{extension.get('extensionId', 'unknown')}",
            "extensionId": extension.get("extensionId"),
            "sourceVersion": source_version,
            "targetVersion": target_version,
            "ruleId": rule_id,
            "status": status,
            "loss": losses,
            "losses": losses,
            "droppedFields": dropped_fields,
        })
    return migrated, receipts


def migrate_document(document: dict[str, Any], target_version: str) -> tuple[dict[str, Any], list[dict[str, str]]]:
    """Migrate the bounded canonical wire-version lane fail-closed.

    The current contract has one explicit legacy boundary: ``0.9.0`` to
    ``1.0.0``.  That boundary removes the retired top-level ``legacyField``
    only with an authored loss receipt.  Same-version migration is an
    identity operation, while every future or otherwise unknown target is
    rejected instead of being silently relabelled.
    """

    source_version = document.get("schema", {}).get("version")
    if not isinstance(source_version, str) or not isinstance(target_version, str):
        raise CanonicalizationError("document and target schema versions must be strings")
    # Validate the document's structure against the current authority while
    # allowing the explicitly supported legacy version to reach the migration
    # branch below.  Validating the legacy version verbatim would reject the
    # very input this boundary is responsible for upgrading.
    authority_candidate = copy.deepcopy(document)
    if source_version == "0.9.0" and target_version == "1.0.0":
        authority_candidate.setdefault("schema", {})["version"] = "1.0.0"
    _validate_authority(authority_candidate)
    if target_version == source_version:
        return copy.deepcopy(document), []
    if source_version == "0.9.0" and target_version == "1.0.0":
        migrated = copy.deepcopy(document)
        migrated.setdefault("schema", {})["version"] = target_version
        migrated.pop("legacyField", None)
        return migrated, [
            {
                "receiptId": "loss-legacy-future-field",
                "fieldPath": "$.legacyField",
                "disposition": "omitted",
                "diagnosticCode": "DFIR-MIGRATION-LOSS",
            }
        ]
    raise CanonicalizationError(f"no registered migration from {source_version} to {target_version}")


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
