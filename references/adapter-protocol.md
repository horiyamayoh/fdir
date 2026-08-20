# Adapter protocol, isolation, and resource boundary

Issue #12 implements the development-unqualified adapter boundary used by Rust and non-Rust workers. The boundary is language-neutral: authority comes from exact artifact identity, worker manifests, evidence lanes, source links, receipts, and qualification evidence, never from the worker implementation language.

This implementation does **not** add a document adapter or qualify a format/capability/profile tuple for production. `fdir-adapter-sdk::CAPABILITY` is available because the protocol contract is executable, but `production_ready` remains false. First-party format adapters, exhaustive accounting, dependency qualification, reliability, and security qualification remain owned by later roadmap issues.

## Versioned authorities

| Authority | Current value |
|---|---|
| Envelope schema | `fdir/adapter-protocol/1` |
| Protocol version | `1.0.0` |
| Worker manifest | `fdir/adapter-worker-manifest/1` |
| Sandbox receipt | `fdir/adapter-sandbox-receipt/1` |
| Rust implementation | `crates/fdir-adapter-sdk` |
| Python oracle and process harness | `tools/adapter_protocol.py` |
| Non-Rust conformance worker | `tools/mock_adapter_worker.py` |
| Shared vectors | `fixtures/adapter-protocol/` |
| Machine conformance declaration | `quality/adapter-protocol.json` |

All JSON objects are closed. Unknown fields, unknown critical fields, duplicate members, malformed values, unsupported versions, and unsupported message kinds fail before a body is used. The Rust decoder and Python oracle consume the same vectors. No implicit migration or best-effort interpretation exists.

## Negotiation and identity

The coordinator offers an exact set of protocol versions plus a capability, profile, required evidence lanes, and an opaque artifact handle. The worker manifest is content-addressed and declares exact worker/build/dependency versions, features, normalizations, unavailable source distinctions, unsafe/FFI/native-code facts, process boundary, network policy, deterministic behavior, capabilities, profiles, lanes, qualification state, and owning issue.

Negotiation succeeds only when:

1. both parties explicitly support `1.0.0`;
2. the requested capability and profile occur in the exact manifest;
3. every requested lane is a subset of that capability declaration;
4. the artifact handle, digest, byte length, and media type are valid;
5. a requested production qualification is actually present in both the worker and capability declaration.

A session binds the protocol version, worker ID/version, manifest digest, capability, profile, lanes, and artifact identity. Execution adds configuration and optional context digests. The deterministic replay key is a length-prefixed encoding of those identity facts, so concatenation ambiguity, timestamps, launch IDs, and retry counters cannot alter idempotency. A retry whose identity differs is a new request and fails the previous replay comparison.

Artifacts cross the boundary only as `artifact:` handles plus digest and byte length. Host paths, path traversal, arbitrary command strings, ambient credentials, and undeclared executable paths are not protocol values.

## Evidence-lane separation

The wire uses the exact ADR 0004 lane vocabulary:

| Lane | Wire receipt | Required link or identity |
|---|---|---|
| `native-substrate-census` | Native inventory item, selector, evidence digest | Exact native selector and evidence identity |
| `semantic-helper` | Candidate ID and value | At least one source occurrence |
| `renderer-observation` | Observation and exact renderer version | At least one source occurrence |
| `ocr-inference-observation` | Observation, method, confidence, and value | At least one source occurrence |
| `storage-codec` | Object digest and byte length | Exact object identity |

Rust represents these as different `LaneOutput` variants. The JSON schema uses a lane-discriminated `oneOf`, and both validators apply a closed field set for the selected lane. A semantic candidate cannot carry native census fields, a renderer or OCR observation cannot overwrite native evidence, and a storage codec cannot emit an interpretation payload.

## Streaming, budgets, and backpressure

Every execution declares positive ceilings for CPU time, peak memory, output bytes, object count, recursion depth, decompression ratio, wall-clock duration, temporary storage, chunk size, and in-flight chunks. Worker usage is cumulative and cannot move backward. Arithmetic overflow is treated as a limit violation.

Chunks have a monotonic zero-based sequence, a positive byte length, a payload digest, a final marker, and an evidence lane. The coordinator rejects gaps, duplicates, chunks after the final marker, chunks larger than the per-chunk budget, total output beyond the sink budget, and more unacknowledged chunks than the negotiated window. A result cannot be complete until the final chunk has been received and every admitted chunk acknowledged.

These checks are deliberately duplicated at the coordinator/protocol layer and the production launcher boundary. A worker report does not replace launcher-enforced CPU, memory, wall-clock, filesystem, or output-sink limits.

## Cancellation, crash, and durable outcomes

Cancellation changes a running session to `cancelling`; it cannot later become `complete`. A session accepts exactly one terminal receipt and rejects output or usage changes after terminal state. Terminal receipts are bound to the request ID, artifact digest, manifest digest, cumulative usage, exact worker/build/configuration/platform provenance, and dependency IDs.

The protocol retains these durable outcomes without a lossy success flag:

`complete`, `partial`, `unsupported`, `unresolved`, `cancelled`, `failed`, `unreadable`, `resource-limited`, `policy-excluded`, `timed-out`, `worker-crash`, `sandbox-denied`, `protocol-mismatch`, `identity-mismatch`, `malformed-response`, and `truncated-output`.

Only `complete` maps to complete kernel state. Timeout remains resource-limited, sandbox denial remains policy-excluded, truncated output remains partial, and crash/protocol/identity/malformed failures remain failed while preserving their more specific protocol outcome. A successful process exit without a valid terminal receipt is still a failure.

## Production launcher contract

The SDK never treats process creation alone as isolation. A worker that receives untrusted bytes through non-Rust code, unsafe code, FFI, or native code must be registered as `isolated-worker`. The registry resolves an opaque executable ID to exact executable and manifest digests; the request does not supply a host executable path.

Before any worker output is trusted, a supported production launcher must return an exact `fdir/adapter-sandbox-receipt/1` bound to the worker, manifest, executable, and policy digests. The receipt must attest all of the following:

- network denied;
- only opaque read-only artifact handles exposed;
- isolated temporary storage;
- inherited environment and credentials cleared;
- child process creation denied;
- input mounted or exposed read-only;
- CPU, memory, output, object, recursion, decompression, wall-clock, and temporary-storage limits enforced.

The strict policy has no permissive production mode. Any false or missing control yields `sandbox-denied`; no worker output is accepted. The Python mock harness proves process separation, a minimal environment, isolated working state, timeout/cancellation, crash, malformed/truncated responses, output limits, identity/lane mismatch, and deterministic replay. It is a conformance worker, not a production sandbox. Platform-specific launcher qualification and adversarial security evidence remain required by Issue #23 before any format tuple can become production-qualified.

## Compatibility and change control

Version negotiation is exact:

| Coordinator | Worker | Result |
|---|---|---|
| `1.0.0` | `1.0.0` | accepted if manifest/capability/profile/lane checks pass |
| `1.0.0` | `2.0.0` | `protocol-mismatch` |
| `2.0.0` | `1.0.0` | `protocol-mismatch` |

A compatible extension requires a new explicitly offered protocol version. Adding a field to a current closed object, changing lane meaning, weakening identity binding, or accepting unknown critical data is not backward-compatible. Logical-model changes still require the repository's normative change-control process; protocol revisions cannot silently change FDIR 2.1 authority.

## Verification

Focused standard-library checks are:

```bash
python3 -m unittest -v tests.test_adapter_protocol
python3 tools/adapter_protocol.py envelope fixtures/adapter-protocol/valid-output.json
python3 tools/adapter_protocol.py manifest fixtures/adapter-protocol/worker-manifest.json
python3 tools/adapter_protocol.py sandbox fixtures/adapter-protocol/sandbox-receipt.json
```

The authoritative integration command also builds, formats, lints, and tests the Rust SDK and runs the Python mock-process suite:

```bash
python3 tools/quality.py --mode full --cache-policy off .
```
