#!/usr/bin/env python3
"""Canonical JSON and digest helpers for the FDIR 2.1 baseline."""
from __future__ import annotations

import hashlib
import json
import math
from typing import Any

CANONICAL_JSON_VERSION = "fdir-canonical-json/1"
IDENTITY_VERSION = "fdir-identity/1"
MIN_INTEGER = -(2**63)
MAX_INTEGER = 2**64 - 1


class CanonicalError(ValueError):
    """Stable canonicalization failure with a machine-readable code and path."""

    def __init__(self, code: str, path: str, message: str) -> None:
        super().__init__(f"{code} at {path}: {message}")
        self.code = code
        self.path = path
        self.message = message


def _pointer_token(value: str) -> str:
    return value.replace("~", "~0").replace("/", "~1")


def _validate(value: Any, path: str = "$") -> None:
    if value is None or isinstance(value, (bool, str)):
        return
    if isinstance(value, int):
        if value < MIN_INTEGER or value > MAX_INTEGER:
            raise CanonicalError(
                "FDIR-CANONICAL-INTEGER-RANGE",
                path,
                f"integer must be between {MIN_INTEGER} and {MAX_INTEGER}",
            )
        return
    if isinstance(value, float):
        if not math.isfinite(value):
            raise CanonicalError(
                "FDIR-CANONICAL-NUMBER-NON-FINITE",
                path,
                "canonical JSON forbids NaN and infinity",
            )
        return
    if isinstance(value, list):
        for index, item in enumerate(value):
            _validate(item, f"{path}/{index}")
        return
    if isinstance(value, dict):
        for key, item in value.items():
            if not isinstance(key, str):
                raise CanonicalError(
                    "FDIR-CANONICAL-OBJECT-KEY",
                    path,
                    "canonical JSON object keys must be strings",
                )
            _validate(item, f"{path}/{_pointer_token(key)}")
        return
    raise CanonicalError(
        "FDIR-CANONICAL-TYPE",
        path,
        f"unsupported canonical JSON value type: {type(value).__name__}",
    )


def canonical_bytes(value: Any) -> bytes:
    """Serialize one supported value exactly as the frozen Python authority."""

    _validate(value)
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def canonical_text(value: Any) -> str:
    """Return canonical JSON as text."""

    return canonical_bytes(value).decode("utf-8")


def _parse_integer(raw: str) -> int:
    value = int(raw)
    _validate(value)
    return value


def _parse_float(raw: str) -> float:
    value = float(raw)
    if not math.isfinite(value):
        raise CanonicalError(
            "FDIR-CANONICAL-NUMBER-NON-FINITE",
            "$",
            "JSON number is outside the finite binary64 range",
        )
    if value == 0.0 and any(character in "123456789" for character in raw):
        raise CanonicalError(
            "FDIR-CANONICAL-NUMBER-UNDERFLOW",
            "$",
            "JSON number underflows finite binary64",
        )
    return value


def _reject_constant(raw: str) -> Any:
    raise CanonicalError(
        "FDIR-CANONICAL-NUMBER-NON-FINITE",
        "$",
        f"non-standard JSON number is forbidden: {raw}",
    )


def _object_from_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise CanonicalError(
                "FDIR-CANONICAL-DUPLICATE-KEY",
                f"$/{_pointer_token(key)}",
                f"duplicate object key: {key}",
            )
        result[key] = value
    return result


def parse_json(value: str) -> Any:
    """Parse JSON without duplicate keys, non-finite numbers, or range loss."""

    try:
        parsed = json.loads(
            value,
            parse_int=_parse_integer,
            parse_float=_parse_float,
            parse_constant=_reject_constant,
            object_pairs_hook=_object_from_pairs,
        )
    except CanonicalError:
        raise
    except json.JSONDecodeError as error:
        raise CanonicalError(
            "FDIR-CANONICAL-JSON-SYNTAX",
            "$",
            f"JSON syntax error at byte {error.pos}: {error.msg}",
        ) from error
    _validate(parsed)
    return parsed


def canonicalize_json(value: str) -> bytes:
    """Parse and canonicalize one JSON text value."""

    return canonical_bytes(parse_json(value))


def is_canonical_json(value: str) -> bool:
    """Return whether input is already the exact canonical UTF-8 spelling."""

    return canonicalize_json(value) == value.encode("utf-8")


def sha256_digest(value: Any) -> str:
    """Compute the frozen plain SHA-256 content digest over canonical bytes."""

    return "sha256:" + hashlib.sha256(canonical_bytes(value)).hexdigest()


def sha256_digest_json(value: str) -> str:
    """Compute a content digest after strict JSON parsing and canonicalization."""

    return "sha256:" + hashlib.sha256(canonicalize_json(value)).hexdigest()


def domain_separated_digest(kind: str, value: Any) -> str:
    """Compute an FDIR identity digest over one canonical identity envelope."""

    if not kind or "\0" in kind or not kind.isascii():
        raise CanonicalError(
            "FDIR-IDENTITY-DOMAIN",
            "$",
            "identity kind must be non-empty and cannot contain NUL",
        )
    preimage = b"\0".join(
        (
            b"FDIR-ID",
            IDENTITY_VERSION.encode("ascii"),
            kind.encode("ascii"),
            canonical_bytes(value),
        )
    )
    return "sha256:" + hashlib.sha256(preimage).hexdigest()
