#!/usr/bin/env python3
"""Non-Rust adapter worker used only by the language-neutral conformance harness."""

from __future__ import annotations

import json
import os
import sys
import time
from typing import Any

PROTOCOL_SCHEMA = "fdir/adapter-protocol/1"
PROTOCOL_VERSION = "1.0.0"
BUILD_DIGEST = "sha256:dddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddd"
CONFIG_DIGEST = "sha256:cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc"
EVIDENCE_DIGEST = "sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
MAX_REQUEST_BYTES = 1_048_576


def emit(value: Any, *, newline: bool = True) -> None:
    text = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    sys.stdout.write(text)
    if newline:
        sys.stdout.write("\n")
    sys.stdout.flush()


def envelope(
    request: dict[str, Any], kind: str, sequence: int, body: dict[str, Any]
) -> dict[str, Any]:
    return {
        "schema": PROTOCOL_SCHEMA,
        "protocolVersion": PROTOCOL_VERSION,
        "kind": kind,
        "sessionId": request["sessionId"],
        "requestId": request["requestId"],
        "sequence": sequence,
        "critical": ["protocolVersion", "kind", "requestId", "body"],
        "body": body,
    }


def usage(output_bytes: int, chunks: int) -> dict[str, int]:
    return {
        "cpuMillis": 1,
        "peakMemoryBytes": 1024,
        "outputBytes": output_bytes,
        "objectCount": 1,
        "recursionDepth": 1,
        "compressedInputBytes": 12,
        "decompressedBytes": 12,
        "wallClockMillis": 1,
        "temporaryStorageBytes": 0,
        "emittedChunks": chunks,
    }


def provenance() -> dict[str, Any]:
    return {
        "workerId": "mock-python-worker",
        "workerVersion": "1.0.0",
        "buildDigest": BUILD_DIGEST,
        "configurationDigest": CONFIG_DIGEST,
        "platform": "python-conformance",
        "dependencyIds": ["python-runtime"],
    }


def terminal_body(
    request: dict[str, Any],
    outcome: str = "complete",
    diagnostic: str = "FDIR-WORKER-COMPLETE",
) -> dict[str, Any]:
    return {
        "outcome": outcome,
        "artifactDigest": request["body"]["artifact"]["digest"],
        "manifestDigest": request["body"]["manifestDigest"],
        "outputComplete": outcome == "complete",
        "retryable": outcome in {"timed-out", "worker-crash", "resource-limited"},
        "usage": usage(128, 1),
        "provenance": provenance(),
        "diagnosticCode": diagnostic,
    }


def read_request() -> dict[str, Any] | None:
    """Read one bounded request; the conformance worker is one-shot by design."""
    raw = sys.stdin.buffer.read(MAX_REQUEST_BYTES + 1)
    if not raw or len(raw) > MAX_REQUEST_BYTES:
        print("request is empty or exceeds the conformance limit", file=sys.stderr)
        return None
    try:
        request = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        print(f"request is not valid UTF-8 JSON: {error}", file=sys.stderr)
        return None
    if not isinstance(request, dict):
        print("request must be a JSON object", file=sys.stderr)
        return None
    return request


def run(scenario: str) -> int:
    if scenario == "environment":
        emit(
            {
                "cwd": os.getcwd(),
                "environmentKeys": sorted(os.environ),
                "homePresent": "HOME" in os.environ,
                "credentialKeys": sorted(
                    key
                    for key in os.environ
                    if any(
                        token in key.upper()
                        for token in ("TOKEN", "SECRET", "PASSWORD")
                    )
                ),
            }
        )
        return 0
    if scenario in {"timeout", "cancel"}:
        time.sleep(5)
        return 0
    if scenario == "crash":
        return 23
    request = read_request()
    if request is None:
        return 2
    if scenario == "resource":
        sys.stdout.write("x" * 65_536)
        sys.stdout.flush()
        return 0
    if scenario == "malformed":
        sys.stdout.write("not-json\n")
        sys.stdout.flush()
        return 0
    output = envelope(
        request,
        "output",
        0,
        {
            "lane": "native-substrate-census",
            "inventoryItemId": "item-1",
            "selector": {"byteOffset": 0, "byteLength": 12},
            "evidenceDigest": EVIDENCE_DIGEST,
        },
    )
    terminal = envelope(request, "terminal", 1, terminal_body(request))
    if scenario == "protocol-mismatch":
        output["protocolVersion"] = "2.0.0"
    elif scenario == "identity-mismatch":
        terminal["body"]["artifactDigest"] = (
            "sha256:ffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffff"
        )
    elif scenario == "lane-mismatch":
        output["body"] = {
            "lane": "semantic-helper",
            "candidateId": "candidate-1",
            "sourceOccurrenceIds": ["occurrence-1"],
            "value": "candidate",
        }
    elif scenario == "sandbox-denied":
        terminal["body"] = terminal_body(
            request, "sandbox-denied", "FDIR-WORKER-SANDBOX-DENIED"
        )
    if scenario == "truncated":
        emit(output, newline=False)
        return 0
    emit(output)
    emit(terminal)
    return 0


def main() -> int:
    if len(sys.argv) != 2:
        print("usage: mock_adapter_worker.py SCENARIO", file=sys.stderr)
        return 2
    return run(sys.argv[1])


if __name__ == "__main__":
    raise SystemExit(main())
