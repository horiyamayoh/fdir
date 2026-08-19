# Rust crates

The crate graph and responsibility boundaries are defined in [`../RUST.md`](../RUST.md) and machine-checked against [`../quality/rust-workspace.json`](../quality/rust-workspace.json).

All crates forbid unsafe code and inherit strict workspace lints. The foundation has no external Rust package dependencies and intentionally exposes no successful document-processing path.
