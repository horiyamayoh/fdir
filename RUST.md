# Rust reference-product foundation

This document describes the Issue #7 Rust foundation and the unqualified product boundaries implemented through Issues #8, #9, #10, #11, and #12. It does not claim that any document format adapter, projection, equivalence operation, or release tuple is production-ready. Canonical identity, authoritative storage, the rebuildable SQLite index, and the strict adapter protocol are implemented development boundaries, not qualification evidence.

## Pinned toolchain

`rust-toolchain.toml` is authoritative for Rust 1.97.1, the minimal profile, `rustfmt`, Clippy, and the `x86_64-unknown-linux-gnu` CI target. `Cargo.toml` records Rust 1.97.1 as the minimum supported version and uses Rust edition 2024. `Cargo.lock` is committed and pins the exact Issue #11 dependency admission recorded in `machine/dependency-catalog.yaml`.

The repository-level quality command remains:

```bash
python3 tools/quality.py --mode full --cache-policy off .
```

Full mode discovers `tests/test_rust_workspace.py`. That integration test fails closed when the pinned toolchain, generated Rust contract, workspace graph, lock file, Rust tests, formatter, build, or strict Clippy run is missing or unsuccessful. It writes `reports/quality/rust-workspace.json`; the ordinary repository receipt remains authoritative for the complete run.

Direct developer commands are:

```bash
cargo fmt --all -- --check
cargo build --workspace --all-targets --all-features --locked
cargo clippy --workspace --all-targets --all-features --locked -- -D warnings
cargo test --workspace --all-targets --all-features --locked
cargo run -p fdir-cli -- --help
cargo run -p fdir-cli -- metadata --output json
```

Cargo output is isolated under `.validation/rust-target`. CI may use network access only in the explicit `cargo fetch --locked` acquisition step for admitted dependencies; all authoritative quality, build, Clippy, and test commands run with Cargo offline.

## Acyclic crate graph

The machine-readable graph is `quality/rust-workspace.json`.

| Crate | Direct dependencies | Boundary |
|---|---|---|
| `fdir-contract` | none | Generated neutral model and canonical-vector metadata |
| `fdir-core` | `fdir-contract` | Failure classes, status semantics, build metadata, and capability vocabulary |
| `fdir-canonical` | `fdir-core` | Implemented canonical JSON, digest, and identity-DAG boundary; development-unqualified |
| `fdir-storage` | `fdir-core`, `fdir-canonical`; admitted `rusqlite` | Implemented canonical snapshot/evidence store and rebuildable SQLite materialization; development-unqualified |
| `fdir-adapter-sdk` | `fdir-core` | Implemented strict versioned protocol, identity/lane/replay/resource state machine, and fail-closed launcher receipt contract; development-unqualified |
| `fdir-accounting` | `fdir-core` | Exhaustive-accounting boundary; implementation unavailable |
| `fdir-adapters` | `fdir-core`, `fdir-adapter-sdk` | Empty first-party adapter registry |
| `fdir-semantics` | `fdir-core` | Projection, equivalence, alignment, and lineage boundaries; unavailable |
| `fdir-coordinator` | the six product boundaries plus `fdir-core` | Configuration, metadata aggregation, telemetry, and redaction conventions |
| `fdir-test-support` | `fdir-core` | Deterministic clock/RNG and isolated temporary-store fixtures |
| `fdir-cli` | `fdir-core`, `fdir-coordinator`; test-only `fdir-test-support` | Stable help/version/metadata interface and structured failures |

Dependencies flow downward only. Format names and adapter-specific vocabulary are forbidden in `fdir-core`. There is no document parser, renderer, OCR engine, or unsafe block in workspace source. Issue #11 admits the exact bundled SQLite dependency only for canonical storage-codec materialization; it receives no original document bytes and creates no source-authority or production claim. Issue #12 adds no runtime dependency and registers no first-party adapter.

## Canonical snapshot and object-store rules

`fdir-storage` writes current `fdir/snapshot/1` containers as exact canonical JSON and addresses both snapshots and evidence objects by plain SHA-256 content digests. A reader verifies the requested digest before parsing, rejects non-canonical bytes, negotiates the snapshot and canonical-JSON versions without implicit migration, validates the object-reference DAG, preserves explicit non-success states and provenance, and verifies every required object digest and byte length.

Mutation transactions use a store-local exclusive lock. New bytes are synchronized to a temporary file and atomically renamed to their final content-addressed path. Temporary paths and stale locks are never accepted as complete state; cleanup requires the explicit recovery API. Portable exports write their completion marker last and are published by directory rename. Imports verify the marker, snapshot identity, canonical bytes, and every object before publishing the snapshot.

Snapshot retention is explicit. Garbage collection treats every retained snapshot as a root, fails closed when any retained snapshot or required object is invalid, and can either report or delete only objects unreachable from all roots. Deleting a snapshot never silently deletes evidence; a subsequent explicit garbage-collection pass performs that action.

## Rebuildable SQLite materialization

`fdir-storage` can materialize a canonical snapshot into the generated, versioned SQLite schema in `schemas/fdir.sql`. The database is disposable: every trusted open verifies the application identifier, schema and materializer versions, DDL digest, bound snapshot digest, exact canonical root, SQLite integrity, and every derived table against a fresh canonical traversal. Clean, full, incremental, and delete-then-rebuild paths expose the same canonicalized projection and supported query rows; build mode and generation remain operational metadata outside canonical identity.

The supported consistency queries cover units, assertions, evidence objects and links, relations, guarantee statuses and transitions, capabilities, profiles, diagnostics, provenance, and explicit non-complete outcomes. Missing, corrupt, stale, wrong-version, wrong-snapshot, or logically divergent indexes fail closed. See `references/sqlite-index.md` for the authority and invalidation boundary.

## Adapter protocol and process boundary

`fdir-adapter-sdk` implements protocol `1.0.0` with closed JSON envelopes, exact version and capability negotiation, content-bound opaque artifact handles, lane-discriminated outputs, deterministic replay identity, ordered streaming with acknowledgements, cumulative resource checks, cancellation, and durable crash/timeout/sandbox/protocol/identity/malformed/truncated outcomes. The same fixtures are consumed by Rust tests and the CPython standard-library oracle.

Worker manifests retain exact build and dependency facts, features, lanes, normalizations and unavailable distinctions, unsafe/FFI/native-code exposure, process and network policy, supported capabilities/profiles, determinism, qualification state, and issue ownership. Non-Rust or native workers receiving untrusted document bytes must use the isolated-worker boundary. Production launch acceptance additionally requires a content-bound sandbox receipt attesting network denial, opaque read-only handles, isolated temporary state, cleared environment and credentials, denied child processes, and enforced resource limits. Missing or relaxed controls fail closed.

The process conformance worker is intentionally non-Rust and exercises deterministic replay, identity and lane mismatch, cancellation, timeout, crash, malformed and truncated responses, output limits, minimal environment, and isolated working state. It does not qualify a platform sandbox or document format. See `references/adapter-protocol.md`, the three `machine/adapter-*.schema.json` contracts, and `quality/adapter-protocol.json`.

## CLI and failure semantics

The `fdir` binary exposes only foundation operations: `metadata`, `capabilities`, `status-codes`, and `validate-config`. Conversion, extraction, persistence, projection, equivalence, and lineage commands remain absent. `capabilities` reports implemented boundaries as available but non-production and incomplete boundaries as unavailable.

Exit codes are stable:

| Class | Exit code |
|---|---:|
| complete success | 0 |
| usage | 2 |
| validation | 3 |
| unsupported | 4 |
| partial | 5 |
| policy | 6 |
| resource limit | 7 |
| cancellation | 8 |
| internal | 70 |

Errors carry a stable code, class, message, and non-production marker. Text is written to standard error; `--output json` emits a structured JSON object.

Configuration files use deterministic `key=value` lines with `output`, `log_level`, `redact_paths`, and `deterministic_seed`. Unknown keys and malformed values are validation failures. Logs must not contain source bytes, credentials, or unredacted paths; the coordinator centralizes path redaction and exposes no ambient network or command-execution facility.

## Generated parity

`tools/generate_rust_contract.py` reads `machine/logical-model.yaml` and `fixtures/canonical/vector.json`, then generates `crates/fdir-contract/src/generated.rs` plus a digest manifest. The Python integration check proves byte parity, while the Rust `generated_contract_parity` test independently binds the compiled constants and canonical vector to those same repository authorities.
