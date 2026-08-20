#!/usr/bin/env python3
"""Strict standard-library oracle and mock-process harness for Issue #12."""

from __future__ import annotations

import dataclasses
import json
import os
import subprocess
import sys
import tempfile
from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path
from typing import Any

PROTOCOL_SCHEMA = "fdir/adapter-protocol/1"
PROTOCOL_VERSION = "1.0.0"
MANIFEST_SCHEMA = "fdir/adapter-worker-manifest/1"
SANDBOX_RECEIPT_SCHEMA = "fdir/adapter-sandbox-receipt/1"
LANES = frozenset(
    {
        "native-substrate-census",
        "semantic-helper",
        "renderer-observation",
        "ocr-inference-observation",
        "storage-codec",
    }
)
OUTCOMES = frozenset(
    {
        "complete",
        "partial",
        "unsupported",
        "unresolved",
        "cancelled",
        "failed",
        "unreadable",
        "resource-limited",
        "policy-excluded",
        "timed-out",
        "worker-crash",
        "sandbox-denied",
        "protocol-mismatch",
        "identity-mismatch",
        "malformed-response",
        "truncated-output",
    }
)
DIGEST_PREFIX = "sha256:"
DIGEST_LENGTH = len(DIGEST_PREFIX) + 64


class ProtocolViolation(ValueError):
    """Fail-closed protocol violation with a stable code."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message


@dataclasses.dataclass(frozen=True)
class ProcessOutcome:
    """Durable classification returned by the mock process harness."""

    outcome: str
    diagnostic_code: str
    stdout: bytes
    stderr: bytes
    envelopes: tuple[dict[str, Any], ...] = ()


def load_json(path: Path) -> Any:
    """Load JSON and reject duplicate object keys."""

    def pairs(values: list[tuple[str, Any]]) -> dict[str, Any]:
        output: dict[str, Any] = {}
        for key, value in values:
            if key in output:
                raise ProtocolViolation(
                    "FDIR-PROTOCOL-DUPLICATE-FIELD",
                    f"duplicate JSON object member {key!r}",
                )
            output[key] = value
        return output

    return json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=pairs)


def canonical_line(value: Any) -> bytes:
    """Render deterministic UTF-8 JSON followed by one newline."""

    return (
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        + "\n"
    ).encode("utf-8")


def replay_key(values: Iterable[str]) -> str:
    """Return the language-neutral length-prefixed idempotency key material."""

    return "".join(f"{len(value)}:{value}|" for value in values)


def validate_envelope(value: Any) -> dict[str, Any]:
    """Validate one strict adapter wire envelope and its lane-specific body."""

    envelope = _strict_object(
        value,
        {
            "schema",
            "protocolVersion",
            "kind",
            "sessionId",
            "requestId",
            "sequence",
            "critical",
            "body",
        },
        "FDIR-PROTOCOL-UNKNOWN-FIELD",
    )
    if _string(envelope, "schema") != PROTOCOL_SCHEMA:
        raise ProtocolViolation(
            "FDIR-PROTOCOL-SCHEMA-MISMATCH", "unsupported protocol schema"
        )
    if _string(envelope, "protocolVersion") != PROTOCOL_VERSION:
        raise ProtocolViolation(
            "FDIR-PROTOCOL-VERSION-MISMATCH", "unsupported protocol version"
        )
    kind = _string(envelope, "kind")
    if kind not in {
        "client-hello",
        "worker-hello",
        "execute",
        "cancel",
        "chunk",
        "output",
        "terminal",
    }:
        raise ProtocolViolation(
            "FDIR-PROTOCOL-MESSAGE-KIND", f"unknown message kind {kind!r}"
        )
    _non_empty_string(envelope, "sessionId")
    _non_empty_string(envelope, "requestId")
    _integer(envelope, "sequence")
    critical = _string_list(envelope, "critical", allow_empty=True, unique=True)
    body = _mapping(envelope, "body")
    allowed_body = _validate_body(kind, body)
    for field in critical:
        if field.startswith("body."):
            body_field = field.removeprefix("body.")
            if body_field not in allowed_body or body_field not in body:
                raise ProtocolViolation(
                    "FDIR-PROTOCOL-UNKNOWN-CRITICAL-FIELD",
                    f"unknown or absent critical field {field!r}",
                )
        elif field not in envelope:
            raise ProtocolViolation(
                "FDIR-PROTOCOL-UNKNOWN-CRITICAL-FIELD",
                f"unknown or absent critical field {field!r}",
            )
    return envelope


def validate_worker_manifest(value: Any) -> dict[str, Any]:
    """Validate exact worker, capability, dependency, lane, and isolation facts."""

    manifest = _strict_object(
        value,
        {
            "schema",
            "id",
            "name",
            "version",
            "buildDigest",
            "implementationLanguage",
            "protocolVersions",
            "lanes",
            "capabilities",
            "dependencies",
            "normalizations",
            "unavailableSourceDistinctions",
            "unsafeCode",
            "ffi",
            "nativeCode",
            "receivesUntrustedDocumentBytes",
            "processBoundary",
            "networkPolicy",
            "qualificationState",
            "deterministic",
            "ownerIssue",
        },
        "FDIR-PROTOCOL-MANIFEST-FIELD",
    )
    if _string(manifest, "schema") != MANIFEST_SCHEMA:
        raise ProtocolViolation(
            "FDIR-PROTOCOL-MANIFEST-SCHEMA", "unsupported worker manifest schema"
        )
    worker_id = _non_empty_string(manifest, "id")
    _manifest_id(worker_id)
    _non_empty_string(manifest, "name")
    _exact_version(_non_empty_string(manifest, "version"))
    _digest(manifest, "buildDigest")
    language = _non_empty_string(manifest, "implementationLanguage")
    versions = _string_list(manifest, "protocolVersions", unique=True)
    lanes = set(_lane_list(manifest, "lanes"))
    capabilities = _list(manifest, "capabilities")
    dependencies = _list(manifest, "dependencies")
    _string_list(manifest, "normalizations", allow_empty=True)
    _string_list(manifest, "unavailableSourceDistinctions", allow_empty=True)
    unsafe_code = _boolean(manifest, "unsafeCode")
    ffi = _boolean(manifest, "ffi")
    native_code = _boolean(manifest, "nativeCode")
    receives_untrusted = _boolean(manifest, "receivesUntrustedDocumentBytes")
    boundary = _string(manifest, "processBoundary")
    if boundary not in {
        "trusted-core",
        "in-process",
        "isolated-worker",
        "external-service-forbidden",
    }:
        raise ProtocolViolation(
            "FDIR-PROTOCOL-PROCESS-BOUNDARY", "unknown process boundary"
        )
    network = _string(manifest, "networkPolicy")
    if network not in {"deny", "allowlisted", "required"}:
        raise ProtocolViolation(
            "FDIR-PROTOCOL-WORKER-NETWORK", "unknown network policy"
        )
    qualification = _qualification(_string(manifest, "qualificationState"))
    _boolean(manifest, "deterministic")
    if _integer(manifest, "ownerIssue") <= 0:
        raise ProtocolViolation(
            "FDIR-PROTOCOL-WORKER-OWNER", "owner issue must be positive"
        )
    if not versions or not lanes or not capabilities:
        raise ProtocolViolation(
            "FDIR-PROTOCOL-WORKER-SCOPE",
            "worker must declare versions, lanes, and capabilities",
        )
    capability_ids: set[str] = set()
    for capability_value in capabilities:
        capability = _strict_object(
            capability_value,
            {"id", "profiles", "lanes", "qualificationState"},
            "FDIR-PROTOCOL-CAPABILITY-FIELD",
        )
        capability_id = _non_empty_string(capability, "id")
        _manifest_id(capability_id)
        if capability_id in capability_ids:
            raise ProtocolViolation(
                "FDIR-PROTOCOL-DUPLICATE-CAPABILITY", capability_id
            )
        capability_ids.add(capability_id)
        profiles = _string_list(capability, "profiles", unique=True)
        capability_lanes = set(_lane_list(capability, "lanes"))
        capability_qualification = _qualification(
            _string(capability, "qualificationState")
        )
        if not profiles or not capability_lanes or not capability_lanes <= lanes:
            raise ProtocolViolation(
                "FDIR-PROTOCOL-CAPABILITY-SCOPE", capability_id
            )
        if _qualification_rank(capability_qualification) > _qualification_rank(
            qualification
        ):
            raise ProtocolViolation(
                "FDIR-PROTOCOL-CAPABILITY-QUALIFICATION", capability_id
            )
    dependency_ids: set[str] = set()
    for dependency_value in dependencies:
        dependency = _strict_object(
            dependency_value,
            {
                "id",
                "version",
                "features",
                "lanes",
                "normalizations",
                "unavailableSourceDistinctions",
                "unsafeCode",
                "ffi",
                "nativeCode",
                "processBoundary",
            },
            "FDIR-PROTOCOL-DEPENDENCY-FIELD",
        )
        dependency_id = _non_empty_string(dependency, "id")
        _manifest_id(dependency_id)
        if dependency_id in dependency_ids:
            raise ProtocolViolation(
                "FDIR-PROTOCOL-DUPLICATE-DEPENDENCY", dependency_id
            )
        dependency_ids.add(dependency_id)
        _exact_version(_non_empty_string(dependency, "version"))
        _string_list(dependency, "features", allow_empty=True, unique=True)
        dependency_lanes = set(_lane_list(dependency, "lanes"))
        if not dependency_lanes <= lanes:
            raise ProtocolViolation(
                "FDIR-PROTOCOL-DEPENDENCY-LANE", dependency_id
            )
        _string_list(dependency, "normalizations", allow_empty=True)
        _string_list(
            dependency, "unavailableSourceDistinctions", allow_empty=True
        )
        dependency_unsafe = _boolean(dependency, "unsafeCode")
        dependency_ffi = _boolean(dependency, "ffi")
        dependency_native = _boolean(dependency, "nativeCode")
        dependency_boundary = _string(dependency, "processBoundary")
        if (dependency_unsafe or dependency_ffi or dependency_native) and (
            dependency_boundary == "in-process"
        ):
            raise ProtocolViolation(
                "FDIR-PROTOCOL-UNSAFE-IN-PROCESS", dependency_id
            )
    if receives_untrusted and (
        language.casefold() != "rust" or unsafe_code or ffi or native_code
    ) and boundary != "isolated-worker":
        raise ProtocolViolation(
            "FDIR-PROTOCOL-WORKER-ISOLATION",
            "untrusted non-Rust or native worker must be isolated",
        )
    if receives_untrusted and network == "required":
        raise ProtocolViolation(
            "FDIR-PROTOCOL-WORKER-NETWORK",
            "untrusted worker cannot require ambient network access",
        )
    return manifest


def validate_sandbox_receipt(
    value: Any, *, worker_id: str, manifest_digest: str, executable_digest: str
) -> dict[str, Any]:
    """Validate the external launcher's exact fail-closed isolation attestation."""

    receipt = _strict_object(
        value,
        {
            "schema",
            "launcherId",
            "launcherVersion",
            "workerId",
            "manifestDigest",
            "executableDigest",
            "policyDigest",
            "networkDenied",
            "opaqueHandlesOnly",
            "isolatedTemporaryStorage",
            "environmentCleared",
            "credentialsCleared",
            "childProcessesDenied",
            "inputReadOnly",
            "resourceLimitsEnforced",
        },
        "FDIR-SANDBOX-RECEIPT-FIELD",
    )
    if _string(receipt, "schema") != SANDBOX_RECEIPT_SCHEMA:
        raise ProtocolViolation(
            "FDIR-SANDBOX-RECEIPT-SCHEMA", "unsupported sandbox receipt schema"
        )
    _non_empty_string(receipt, "launcherId")
    _exact_version(_non_empty_string(receipt, "launcherVersion"))
    if _string(receipt, "workerId") != worker_id:
        raise ProtocolViolation(
            "FDIR-SANDBOX-IDENTITY-MISMATCH", "worker id mismatch"
        )
    if _digest(receipt, "manifestDigest") != manifest_digest:
        raise ProtocolViolation(
            "FDIR-SANDBOX-IDENTITY-MISMATCH", "manifest digest mismatch"
        )
    if _digest(receipt, "executableDigest") != executable_digest:
        raise ProtocolViolation(
            "FDIR-SANDBOX-IDENTITY-MISMATCH", "executable digest mismatch"
        )
    _digest(receipt, "policyDigest")
    required_controls = {
        "networkDenied",
        "opaqueHandlesOnly",
        "isolatedTemporaryStorage",
        "environmentCleared",
        "credentialsCleared",
        "childProcessesDenied",
        "inputReadOnly",
        "resourceLimitsEnforced",
    }
    if any(not _boolean(receipt, field) for field in required_controls):
        raise ProtocolViolation(
            "FDIR-SANDBOX-DENIED", "one or more required isolation controls are absent"
        )
    return receipt


def run_mock_worker(
    scenario: str,
    *,
    max_output_bytes: int = 16_384,
    timeout_seconds: float = 1.0,
    cancel_after_seconds: float | None = None,
) -> ProcessOutcome:
    """Run the non-Rust conformance worker with a minimal environment and bounded sink."""

    repository_root = Path(__file__).resolve().parents[1]
    worker = repository_root / "tools/mock_adapter_worker.py"
    request = load_json(
        repository_root / "fixtures/adapter-protocol/valid-execute.json"
    )
    environment = {
        "FDIR_PROTOCOL_VERSION": PROTOCOL_VERSION,
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "TZ": "UTC",
    }
    with tempfile.TemporaryDirectory(prefix="fdir-adapter-worker-") as temporary:
        process = subprocess.Popen(
            [sys.executable, str(worker), scenario],
            cwd=temporary,
            env=environment,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
        )
        deadline = (
            cancel_after_seconds
            if cancel_after_seconds is not None
            else timeout_seconds
        )
        try:
            stdout, stderr = process.communicate(
                input=canonical_line(request), timeout=deadline
            )
        except subprocess.TimeoutExpired:
            process.kill()
            stdout, stderr = process.communicate()
            if cancel_after_seconds is not None:
                return ProcessOutcome(
                    "cancelled", "FDIR-WORKER-CANCELLED", stdout, stderr
                )
            return ProcessOutcome("timed-out", "FDIR-WORKER-TIMEOUT", stdout, stderr)
    if len(stdout) > max_output_bytes:
        return ProcessOutcome(
            "resource-limited", "FDIR-WORKER-OUTPUT-LIMIT", stdout, stderr
        )
    if process.returncode != 0:
        return ProcessOutcome("worker-crash", "FDIR-WORKER-CRASH", stdout, stderr)
    if stdout and not stdout.endswith(b"\n"):
        return ProcessOutcome(
            "truncated-output", "FDIR-WORKER-TRUNCATED", stdout, stderr
        )
    envelopes: list[dict[str, Any]] = []
    try:
        for raw_line in stdout.splitlines():
            if not raw_line:
                continue
            value = json.loads(raw_line.decode("utf-8"))
            envelopes.append(validate_envelope(value))
    except (UnicodeDecodeError, json.JSONDecodeError, ProtocolViolation) as error:
        if isinstance(error, ProtocolViolation) and error.code == (
            "FDIR-PROTOCOL-VERSION-MISMATCH"
        ):
            return ProcessOutcome(
                "protocol-mismatch", error.code, stdout, stderr, tuple(envelopes)
            )
        return ProcessOutcome(
            "malformed-response",
            "FDIR-WORKER-MALFORMED-RESPONSE",
            stdout,
            stderr,
            tuple(envelopes),
        )
    if not envelopes:
        return ProcessOutcome(
            "truncated-output", "FDIR-WORKER-NO-TERMINAL", stdout, stderr
        )
    request_id = request["requestId"]
    expected_lane = request["body"]["lanes"][0]
    expected_artifact = request["body"]["artifact"]["digest"]
    expected_manifest = request["body"]["manifestDigest"]
    for envelope in envelopes:
        if envelope["requestId"] != request_id:
            return ProcessOutcome(
                "identity-mismatch",
                "FDIR-WORKER-REQUEST-ID-MISMATCH",
                stdout,
                stderr,
                tuple(envelopes),
            )
        if envelope["kind"] == "output" and envelope["body"]["lane"] != expected_lane:
            return ProcessOutcome(
                "identity-mismatch",
                "FDIR-WORKER-LANE-MISMATCH",
                stdout,
                stderr,
                tuple(envelopes),
            )
    terminal = envelopes[-1]
    if terminal["kind"] != "terminal":
        return ProcessOutcome(
            "truncated-output",
            "FDIR-WORKER-NO-TERMINAL",
            stdout,
            stderr,
            tuple(envelopes),
        )
    body = terminal["body"]
    if (
        body["artifactDigest"] != expected_artifact
        or body["manifestDigest"] != expected_manifest
    ):
        return ProcessOutcome(
            "identity-mismatch",
            "FDIR-WORKER-IDENTITY-MISMATCH",
            stdout,
            stderr,
            tuple(envelopes),
        )
    return ProcessOutcome(
        body["outcome"], body["diagnosticCode"], stdout, stderr, tuple(envelopes)
    )


def probe_mock_worker_environment() -> dict[str, Any]:
    """Prove the test harness clears ambient variables and uses isolated temporary state."""

    repository_root = Path(__file__).resolve().parents[1]
    worker = repository_root / "tools/mock_adapter_worker.py"
    environment = {
        "FDIR_PROTOCOL_VERSION": PROTOCOL_VERSION,
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "TZ": "UTC",
    }
    with tempfile.TemporaryDirectory(prefix="fdir-adapter-worker-") as temporary:
        completed = subprocess.run(
            [sys.executable, str(worker), "environment"],
            cwd=temporary,
            env=environment,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=5,
            check=False,
        )
    if completed.returncode != 0:
        raise ProtocolViolation(
            "FDIR-WORKER-ENVIRONMENT-PROBE", completed.stderr.decode("utf-8")
        )
    value = json.loads(completed.stdout.decode("utf-8"))
    if not isinstance(value, dict):
        raise ProtocolViolation(
            "FDIR-WORKER-ENVIRONMENT-PROBE", "probe output is not an object"
        )
    return value


def _validate_body(kind: str, body: Mapping[str, Any]) -> set[str]:
    if kind == "client-hello":
        allowed = {
            "supportedVersions",
            "requestedCapability",
            "profile",
            "requiredLanes",
            "artifact",
        }
        _strict_object(body, allowed, "FDIR-PROTOCOL-CLIENT-HELLO-FIELD")
        versions = _string_list(body, "supportedVersions", unique=True)
        if PROTOCOL_VERSION not in versions:
            raise ProtocolViolation(
                "FDIR-PROTOCOL-VERSION-MISMATCH", "client does not offer version"
            )
        _non_empty_string(body, "requestedCapability")
        _non_empty_string(body, "profile")
        _lane_list(body, "requiredLanes")
        _validate_artifact(_mapping(body, "artifact"))
        return allowed
    if kind == "worker-hello":
        allowed = {
            "selectedVersion",
            "manifestDigest",
            "capabilities",
            "lanes",
            "qualificationState",
            "productionReady",
        }
        _strict_object(body, allowed, "FDIR-PROTOCOL-WORKER-HELLO-FIELD")
        if _string(body, "selectedVersion") != PROTOCOL_VERSION:
            raise ProtocolViolation(
                "FDIR-PROTOCOL-VERSION-MISMATCH", "worker selected wrong version"
            )
        _digest(body, "manifestDigest")
        _string_list(body, "capabilities", unique=True)
        _lane_list(body, "lanes")
        qualification = _qualification(_string(body, "qualificationState"))
        production_ready = _boolean(body, "productionReady")
        if production_ready and qualification != "production-qualified":
            raise ProtocolViolation(
                "FDIR-PROTOCOL-FALSE-PRODUCTION-CLAIM",
                "unqualified worker advertised production-ready",
            )
        return allowed
    if kind == "execute":
        allowed = {
            "manifestDigest",
            "artifact",
            "capability",
            "profile",
            "lanes",
            "budget",
            "configurationDigest",
            "contextDigest",
            "replayKey",
        }
        _strict_object(body, allowed, "FDIR-PROTOCOL-EXECUTE-FIELD")
        _digest(body, "manifestDigest")
        _validate_artifact(_mapping(body, "artifact"))
        _non_empty_string(body, "capability")
        _non_empty_string(body, "profile")
        _lane_list(body, "lanes")
        _validate_budget(_mapping(body, "budget"))
        _digest(body, "configurationDigest")
        context = body["contextDigest"]
        if context is not None:
            _validate_digest_value(context)
        _non_empty_string(body, "replayKey")
        return allowed
    if kind == "cancel":
        allowed = {"reason"}
        _strict_object(body, allowed, "FDIR-PROTOCOL-CANCEL-FIELD")
        _non_empty_string(body, "reason")
        return allowed
    if kind == "chunk":
        allowed = {"lane", "chunkSequence", "byteLength", "final", "payloadDigest"}
        _strict_object(body, allowed, "FDIR-PROTOCOL-CHUNK-FIELD")
        _lane(_string(body, "lane"))
        _integer(body, "chunkSequence")
        if _integer(body, "byteLength") <= 0:
            raise ProtocolViolation(
                "FDIR-PROTOCOL-CHUNK-LENGTH", "chunk must be non-empty"
            )
        _boolean(body, "final")
        _digest(body, "payloadDigest")
        return allowed
    if kind == "output":
        return _validate_output(body)
    if kind == "terminal":
        return _validate_terminal(body)
    raise AssertionError(kind)


def _validate_output(body: Mapping[str, Any]) -> set[str]:
    lane = _lane(_string(body, "lane"))
    if lane == "native-substrate-census":
        allowed = {"lane", "inventoryItemId", "selector", "evidenceDigest"}
        _strict_object(body, allowed, "FDIR-PROTOCOL-NATIVE-OUTPUT-FIELD")
        _non_empty_string(body, "inventoryItemId")
        _digest(body, "evidenceDigest")
        return allowed
    if lane == "semantic-helper":
        allowed = {"lane", "candidateId", "sourceOccurrenceIds", "value"}
        _strict_object(body, allowed, "FDIR-PROTOCOL-SEMANTIC-OUTPUT-FIELD")
        _non_empty_string(body, "candidateId")
        _string_list(body, "sourceOccurrenceIds")
        return allowed
    if lane == "renderer-observation":
        allowed = {
            "lane",
            "observationId",
            "sourceOccurrenceIds",
            "rendererVersion",
            "value",
        }
        _strict_object(body, allowed, "FDIR-PROTOCOL-RENDERER-OUTPUT-FIELD")
        _non_empty_string(body, "observationId")
        _string_list(body, "sourceOccurrenceIds")
        _exact_version(_non_empty_string(body, "rendererVersion"))
        return allowed
    if lane == "ocr-inference-observation":
        allowed = {
            "lane",
            "observationId",
            "sourceOccurrenceIds",
            "method",
            "confidenceMillionths",
            "value",
        }
        _strict_object(body, allowed, "FDIR-PROTOCOL-OCR-OUTPUT-FIELD")
        _non_empty_string(body, "observationId")
        _string_list(body, "sourceOccurrenceIds")
        _non_empty_string(body, "method")
        confidence = _integer(body, "confidenceMillionths")
        if not 0 <= confidence <= 1_000_000:
            raise ProtocolViolation(
                "FDIR-PROTOCOL-OCR-CONFIDENCE", "confidence outside range"
            )
        return allowed
    allowed = {"lane", "objectDigest", "byteLength"}
    _strict_object(body, allowed, "FDIR-PROTOCOL-STORAGE-OUTPUT-FIELD")
    _digest(body, "objectDigest")
    if _integer(body, "byteLength") <= 0:
        raise ProtocolViolation(
            "FDIR-PROTOCOL-STORAGE-LENGTH", "storage output must be non-empty"
        )
    return allowed


def _validate_terminal(body: Mapping[str, Any]) -> set[str]:
    allowed = {
        "outcome",
        "artifactDigest",
        "manifestDigest",
        "outputComplete",
        "retryable",
        "usage",
        "provenance",
        "diagnosticCode",
    }
    _strict_object(body, allowed, "FDIR-PROTOCOL-TERMINAL-FIELD")
    outcome = _string(body, "outcome")
    if outcome not in OUTCOMES:
        raise ProtocolViolation("FDIR-PROTOCOL-OUTCOME", outcome)
    _digest(body, "artifactDigest")
    _digest(body, "manifestDigest")
    output_complete = _boolean(body, "outputComplete")
    retryable = _boolean(body, "retryable")
    _validate_usage(_mapping(body, "usage"))
    _validate_provenance(_mapping(body, "provenance"))
    _non_empty_string(body, "diagnosticCode")
    if outcome == "complete" and (not output_complete or retryable):
        raise ProtocolViolation(
            "FDIR-PROTOCOL-FALSE-COMPLETE", "invalid complete terminal state"
        )
    if outcome == "truncated-output" and output_complete:
        raise ProtocolViolation(
            "FDIR-PROTOCOL-TRUNCATED-COMPLETE", "truncated output marked complete"
        )
    return allowed


def _validate_artifact(value: Mapping[str, Any]) -> None:
    artifact = _strict_object(
        value,
        {"handle", "digest", "byteLength", "mediaType"},
        "FDIR-PROTOCOL-ARTIFACT-FIELD",
    )
    handle = _non_empty_string(artifact, "handle")
    if (
        not handle.startswith("artifact:")
        or "/" in handle
        or "\\" in handle
        or ".." in handle
        or any(character.isspace() for character in handle)
    ):
        raise ProtocolViolation(
            "FDIR-PROTOCOL-ARTIFACT-HANDLE", "artifact handle is path-like"
        )
    _digest(artifact, "digest")
    if _integer(artifact, "byteLength") <= 0:
        raise ProtocolViolation(
            "FDIR-PROTOCOL-ARTIFACT-LENGTH", "artifact must be non-empty"
        )
    _non_empty_string(artifact, "mediaType")


def _validate_budget(value: Mapping[str, Any]) -> None:
    fields = {
        "maxCpuMillis",
        "maxMemoryBytes",
        "maxOutputBytes",
        "maxObjects",
        "maxRecursionDepth",
        "maxDecompressionRatio",
        "maxWallClockMillis",
        "maxTemporaryStorageBytes",
        "maxChunkBytes",
        "maxInFlightChunks",
    }
    budget = _strict_object(value, fields, "FDIR-PROTOCOL-BUDGET-FIELD")
    for field in fields:
        if _integer(budget, field) <= 0:
            raise ProtocolViolation(
                "FDIR-PROTOCOL-BUDGET-NONPOSITIVE", field
            )
    if budget["maxChunkBytes"] > budget["maxOutputBytes"]:
        raise ProtocolViolation("FDIR-PROTOCOL-BUDGET-CHUNK", "chunk exceeds output")


def _validate_usage(value: Mapping[str, Any]) -> None:
    fields = {
        "cpuMillis",
        "peakMemoryBytes",
        "outputBytes",
        "objectCount",
        "recursionDepth",
        "compressedInputBytes",
        "decompressedBytes",
        "wallClockMillis",
        "temporaryStorageBytes",
        "emittedChunks",
    }
    usage = _strict_object(value, fields, "FDIR-PROTOCOL-USAGE-FIELD")
    for field in fields:
        _integer(usage, field)


def _validate_provenance(value: Mapping[str, Any]) -> None:
    provenance = _strict_object(
        value,
        {
            "workerId",
            "workerVersion",
            "buildDigest",
            "configurationDigest",
            "platform",
            "dependencyIds",
        },
        "FDIR-PROTOCOL-PROVENANCE-FIELD",
    )
    _non_empty_string(provenance, "workerId")
    _exact_version(_non_empty_string(provenance, "workerVersion"))
    _digest(provenance, "buildDigest")
    _digest(provenance, "configurationDigest")
    _non_empty_string(provenance, "platform")
    _string_list(provenance, "dependencyIds", allow_empty=True, unique=True)


def _strict_object(
    value: Any, fields: set[str], code: str
) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ProtocolViolation("FDIR-PROTOCOL-FIELD-TYPE", "expected object")
    actual = set(value)
    unknown = sorted(actual - fields)
    missing = sorted(fields - actual)
    if unknown:
        raise ProtocolViolation(code, f"unknown fields: {unknown}")
    if missing:
        raise ProtocolViolation(
            "FDIR-PROTOCOL-MISSING-FIELD", f"missing fields: {missing}"
        )
    return value


def _mapping(value: Mapping[str, Any], field: str) -> dict[str, Any]:
    member = value.get(field)
    if not isinstance(member, dict):
        raise ProtocolViolation(
            "FDIR-PROTOCOL-FIELD-TYPE", f"{field} must be an object"
        )
    return member


def _list(value: Mapping[str, Any], field: str) -> list[Any]:
    member = value.get(field)
    if not isinstance(member, list):
        raise ProtocolViolation(
            "FDIR-PROTOCOL-FIELD-TYPE", f"{field} must be an array"
        )
    return member


def _string(value: Mapping[str, Any], field: str) -> str:
    member = value.get(field)
    if not isinstance(member, str):
        raise ProtocolViolation(
            "FDIR-PROTOCOL-FIELD-TYPE", f"{field} must be a string"
        )
    return member


def _non_empty_string(value: Mapping[str, Any], field: str) -> str:
    member = _string(value, field)
    if not member or any(ord(character) < 32 for character in member):
        raise ProtocolViolation(
            "FDIR-PROTOCOL-FIELD-VALUE", f"{field} must be non-empty"
        )
    return member


def _integer(value: Mapping[str, Any], field: str) -> int:
    member = value.get(field)
    if isinstance(member, bool) or not isinstance(member, int) or member < 0:
        raise ProtocolViolation(
            "FDIR-PROTOCOL-FIELD-TYPE", f"{field} must be a non-negative integer"
        )
    return member


def _boolean(value: Mapping[str, Any], field: str) -> bool:
    member = value.get(field)
    if not isinstance(member, bool):
        raise ProtocolViolation(
            "FDIR-PROTOCOL-FIELD-TYPE", f"{field} must be a boolean"
        )
    return member


def _string_list(
    value: Mapping[str, Any],
    field: str,
    *,
    allow_empty: bool = False,
    unique: bool = False,
) -> list[str]:
    members = _list(value, field)
    if not allow_empty and not members:
        raise ProtocolViolation(
            "FDIR-PROTOCOL-FIELD-VALUE", f"{field} cannot be empty"
        )
    if any(not isinstance(member, str) or not member for member in members):
        raise ProtocolViolation(
            "FDIR-PROTOCOL-FIELD-TYPE", f"{field} must contain non-empty strings"
        )
    output = [str(member) for member in members]
    if unique and len(set(output)) != len(output):
        raise ProtocolViolation(
            "FDIR-PROTOCOL-DUPLICATE-VALUE", f"{field} must be unique"
        )
    return output


def _lane_list(value: Mapping[str, Any], field: str) -> list[str]:
    output = _string_list(value, field, unique=True)
    for lane in output:
        _lane(lane)
    return output


def _lane(value: str) -> str:
    if value not in LANES:
        raise ProtocolViolation("FDIR-PROTOCOL-LANE", value)
    return value


def _digest(value: Mapping[str, Any], field: str) -> str:
    member = _string(value, field)
    _validate_digest_value(member)
    return member


def _validate_digest_value(value: Any) -> None:
    if (
        not isinstance(value, str)
        or len(value) != DIGEST_LENGTH
        or not value.startswith(DIGEST_PREFIX)
        or any(character not in "0123456789abcdef" for character in value[7:])
    ):
        raise ProtocolViolation("FDIR-PROTOCOL-DIGEST", "invalid SHA-256 digest")


def _exact_version(value: str) -> None:
    lowered = value.casefold()
    if (
        not value
        or any(character.isspace() for character in value)
        or any(character in value for character in "*^~<>|")
        or lowered in {"latest", "main", "master", "head", "stable", "nightly"}
        or lowered.endswith(".x")
    ):
        raise ProtocolViolation(
            "FDIR-PROTOCOL-DEPENDENCY-VERSION", "version is not exact"
        )


def _manifest_id(value: str) -> None:
    allowed = set("abcdefghijklmnopqrstuvwxyz0123456789._-")
    if value[0] not in set("abcdefghijklmnopqrstuvwxyz0123456789") or any(
        character not in allowed for character in value
    ):
        raise ProtocolViolation(
            "FDIR-PROTOCOL-MANIFEST-ID", "invalid manifest identifier"
        )


def _qualification(value: str) -> str:
    if value not in {
        "candidate",
        "admitted-unqualified",
        "adapter-qualified",
        "production-qualified",
        "rejected",
    }:
        raise ProtocolViolation("FDIR-PROTOCOL-QUALIFICATION", value)
    return value


def _qualification_rank(value: str) -> int:
    return {
        "rejected": 0,
        "candidate": 1,
        "admitted-unqualified": 2,
        "adapter-qualified": 3,
        "production-qualified": 4,
    }[value]


def main(arguments: Sequence[str] | None = None) -> int:
    """Validate one JSON file for focused diagnostics."""

    values = list(sys.argv[1:] if arguments is None else arguments)
    if len(values) != 2 or values[0] not in {"envelope", "manifest", "sandbox"}:
        print(
            "usage: adapter_protocol.py envelope|manifest|sandbox PATH",
            file=sys.stderr,
        )
        return 2
    value = load_json(Path(values[1]))
    try:
        if values[0] == "envelope":
            validate_envelope(value)
        elif values[0] == "manifest":
            validate_worker_manifest(value)
        else:
            validate_sandbox_receipt(
                value,
                worker_id="mock-python-worker",
                manifest_digest=(
                    "sha256:bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
                ),
                executable_digest=(
                    "sha256:eeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee"
                ),
            )
    except ProtocolViolation as error:
        print(str(error), file=sys.stderr)
        return 3
    print(json.dumps({"status": "complete", "productionReady": False}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
