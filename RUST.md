# Rust reference-product foundation

This document describes the Issue #7 foundation. It establishes buildable product boundaries and developer tooling; it does not claim that any document format, canonical identity implementation, storage engine, adapter, projection, equivalence operation, or release tuple is production-ready.

## Pinned toolchain

`rust-toolchain.toml` is authoritative for Rust 1.97.1, the minimal profile, `rustfmt`, Clippy, and the `x86_64-unknown-linux-gnu` CI target. `Cargo.toml` records Rust 1.97.1 as the minimum supported version and uses Rust edition 2024. `Cargo.lock` is committed even though the foundation intentionally has no external Rust packages.

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

Cargo output is isolated under `.validation/rust-target`, and Cargo network access is disabled at this dependency-free foundation stage.

## Acyclic crate graph

The machine-readable graph is `quality/rust-workspace.json`.

| Crate | Direct dependencies | Boundary |
|---|---|---|
| `fdir-contract` | none | Generated neutral model and canonical-vector metadata |
| `fdir-core` | `fdir-contract` | Failure classes, exit semantics, build metadata, and unavailable-capability vocabulary |
| `fdir-canonical` | `fdir-core` | Canonical JSON and identity boundary; implementation unavailable |
| `fdir-storage` | `fdir-core` | Authoritative storage boundary; implementation unavailable |
| `fdir-adapter-sdk` | `fdir-core` | Protocol and process-isolation metadata only |
| `fdir-accounting` | `fdir-core` | Exhaustive-accounting boundary; implementation unavailable |
| `fdir-adapters` | `fdir-core`, `fdir-adapter-sdk` | Empty first-party adapter registry |
| `fdir-semantics` | `fdir-core` | Projection, equivalence, alignment, and lineage boundaries; unavailable |
| `fdir-coordinator` | the six product boundaries plus `fdir-core` | Configuration, metadata aggregation, telemetry, and redaction conventions |
| `fdir-test-support` | `fdir-core` | Deterministic clock/RNG and isolated temporary-store fixtures |
| `fdir-cli` | `fdir-core`, `fdir-coordinator`; test-only `fdir-test-support` | Stable help/version/metadata interface and structured failures |

Dependencies flow downward only. Format names and adapter-specific vocabulary are forbidden in `fdir-core`. There is no document parser, renderer, OCR engine, FFI dependency, unsafe block, or admitted runtime package.

## CLI and failure semantics

The `fdir` binary exposes only foundation operations: `metadata`, `capabilities`, `status-codes`, and `validate-config`. Conversion, extraction, persistence, projection, equivalence, and lineage commands are absent. `capabilities` reports every incomplete boundary as unavailable and non-production.

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
