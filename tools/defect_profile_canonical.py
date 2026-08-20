"""Direct canonicalization probes for the Issue #89 mutation matrix.

The normal query qualification command intentionally consumes already
canonicalized outputs.  These probes exercise the canonicalizer itself so
authority validation and deterministic ordering mutations cannot hide behind
an output projection that does not contain the affected distinction.
"""

from __future__ import annotations

import argparse
import json
from typing import Any

from canonicalize_ir import canonical_bytes, canonical_value_bytes


class ProbeFailure(RuntimeError):
    pass


def require(condition: bool, message: str) -> None:
    if not condition:
        raise ProbeFailure(message)


def probe_authority() -> None:
    invalid_document = {"schema": {"name": "DocumentFormIR", "version": "1.0.0"}}
    try:
        canonical_bytes(invalid_document)
    except Exception as exc:
        print(f"authority rejected invalid IR: {type(exc).__name__}")
        return
    raise ProbeFailure("canonical_bytes accepted an invalid IR document")


def probe_collection_order() -> None:
    value: dict[str, Any] = {
        "nodes": [
            {"nodeId": "node-b", "value": "second"},
            {"nodeId": "node-c", "value": "third"},
            {"nodeId": "node-a", "value": "first"},
        ]
    }
    normalized = json.loads(canonical_value_bytes(value).decode("utf-8"))
    require([item["nodeId"] for item in normalized["nodes"]] == ["node-a", "node-b", "node-c"], f"entity collection order was not canonical: {normalized!r}")


def probe_key_order() -> None:
    # UTF-16 code-unit order places U+10000 before U+E000, while Python's
    # Unicode code-point and the deliberately wrong variant selectors do not.
    value: dict[str, Any] = {"\uE000": "bmp", "\U00010000": "astral", "aa": "ascii"}
    normalized = json.loads(canonical_value_bytes(value).decode("utf-8"))
    require(list(normalized) == ["aa", "\U00010000", "\uE000"], f"object key order was not UTF-16 canonical: {list(normalized)!r}")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--probe", required=True)
    args = parser.parse_args()
    probe = args.probe.split("-variant-", 1)[0]
    probe = {
        "canonical-authority-validation": "authority",
        "canonical-collection-order": "collection-order",
        "canonical-key-order": "key-order",
    }.get(probe, probe)
    try:
        if probe == "authority":
            probe_authority()
        elif probe == "collection-order":
            probe_collection_order()
        elif probe == "key-order":
            probe_key_order()
        else:
            raise ProbeFailure(f"unknown probe: {args.probe}")
        print(f"probe passed: canonical/{args.probe}")
        return 0
    except Exception as exc:
        print(f"probe failed: canonical/{args.probe}: {type(exc).__name__}: {exc}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
