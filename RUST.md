# Rust reference-product foundation

This document describes the Issue #7 Rust foundation and the unqualified product boundaries implemented through Issues #8, #9, and #10. It does not claim that any document format, adapter, projection, equivalence operation, or release tuple is production-ready. Canonical identity and authoritative storage are implemented development boundaries, not qualification evidence.

## Pinned toolchain

`rust-toolchain.toml` is authoritative for Rust 1.97.1, the minimal profile, `rustfmt`, Clippy, and the `x86_64-unknown-linux-gnu` CI target. `Cargo.toml` records Rust 1.97.1 as the minimum supported version and uses Rust edition 2024. `Cargo.lock` is committed, and the workspace still has no external Rust packages.

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

Cargo output is isolated under `.validation/rust-target`, and Cargo network access remains disabled because every current dependency is an exact workspace path.

## Acyclic crate graph

The machine-readable graph is `quality/rust-workspace.json`.

| Crate | Direct dependencies | Boundary |
|---|---|---|
| `fdir-contract` | none | Generated neutral model and canonical-vector metadata |
| `fdir-core` | `fdir-contract` | Failure classes, status semantics, build metadata, and capability vocabulary |
| `fdir-canonical` | `fdir-core` | Implemented canonical JSON, digest, and identity-DAG boundary; development-unqualified |
| `fdir-storage` | `fdir-core`, `fdir-canonical` | Implemented canonical snapshot and content-addressed evidence store; development-unqualified |
| `fdir-adapter-sdk` | `fdir-core` | Protocol and process-isolation metadata only |
| `fdir-accounting` | `fdir-core` | Exhaustive-accounting boundary; implementation unavailable |
| `fdir-adapters` | `fdir-core`, `fdir-adapter-sdk` | Empty first-party adapter registry |
| `fdir-semantics` | `fdir-core` | Projection, equivalence, alignment, and lineage boundaries; unavailable |
| `fdir-coordinator` | the six product boundaries plus `fdir-core` | Configuration, metadata aggregation, telemetry, and redaction conventions |
| `fdir-test-support` | `fdir-core` | Deterministic clock/RNG and isolated temporary-store fixtures |
| `fdir-cli` | `fdir-core`, `fdir-coordinator`; test-only `fdir-test-support` | Stable help/version/metadata interface and structured failures |

Dependencies flow downward only. Format names and adapter-specific vocabulary are forbidden in `fdir-core`. There is no document parser, renderer, OCR engine, FFI dependency, unsafe block, or admitted external runtime package.

## Canonical snapshot and object-store rules

`fdir-storage` writes current `fdir/snapshot/1` containers as exact canonical JSON and addresses both snapshots and evidence objects by plain SHA-256 content digests. A reader verifies the requested digest before parsing, rejects non-canonical bytes, negotiates the snapshot and canonical-JSON versions without implicit migration, validates the object-reference DAG, preserves explicit non-success states and provenance, and verifies every required object digest and byte length.

Mutation transactions use a store-local exclusive lock. New bytes are synchronized to a temporary file and atomically renamed to their final content-addressed path. Temporary paths and stale locks are never accepted as complete state; cleanup requires the explicit recovery API. Portable exports write their completion marker last and are published by directory rename. Imports verify the marker, snapshot identity, canonical bytes, and every object before publishing the snapshot.

Snapshot retention is explicit. Garbage collection treats every retained snapshot as a root, fails closed when any retained snapshot or required object is invalid, and can either report or delete only objects unreachable from all roots. Deleting a snapshot never silently deletes evidence; a subsequent explicit garbage-collection pass performs that action.

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
