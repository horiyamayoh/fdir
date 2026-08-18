
#!/usr/bin/env python3
"""Canonical JSON helpers for the FDIR 2.1 baseline."""
from __future__ import annotations

import hashlib
import json
import math
from typing import Any


def _reject_non_finite(value: Any) -> None:
    if isinstance(value, float) and not math.isfinite(value):
        raise ValueError("canonical JSON forbids non-finite numbers")
    if isinstance(value, list):
        for item in value:
            _reject_non_finite(item)
    elif isinstance(value, dict):
        for key, item in value.items():
            if not isinstance(key, str):
                raise ValueError("canonical JSON object keys must be strings")
            _reject_non_finite(item)


def canonical_bytes(value: Any) -> bytes:
    _reject_non_finite(value)
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def sha256_digest(value: Any) -> str:
    return "sha256:" + hashlib.sha256(canonical_bytes(value)).hexdigest()
