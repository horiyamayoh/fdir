# FDIR Development and Build Policy

This policy defines how the repository is built and validated while the production implementation is developed. It does not expand the frozen FDIR 2.1 semantics or create a production capability claim.

## 1. Authority classes

| Class | Examples | Rule |
|---|---|---|
| Canonical machine authority | `machine/logical-model.yaml`, requirements, profiles, capabilities, ADRs | Change intentionally, review semantically, and preserve version/change-control rules |
| Generated normative contract | JSON Schema, CDDL, SQLite DDL, generated model reference and traceability | Must be byte-identical to the pinned generator output |
| Normative explanatory source | `spec/**` except generated references | Explains requirements and must not contradict machine authority |
| Generated review artifact | PDF, DOCX, rendered diagrams | Rebuildable review projection; never independent authority |
| Rebuildable product projection | SQLite index, search shard, report, thumbnail | Disposable and reproducible from canonical snapshots/evidence |
| Qualification evidence | corpus manifests, receipts, reports, signatures | Immutable, version-bound evidence; not source semantics by itself |

Deleting a rebuildable projection must not destroy authoritative information. Editing a projection must not alter a canonical identity or production claim.

## 2. Toolchains and dependency locks

- Python baseline tooling currently targets Python 3.12 in CI and uses the standard library unless a requirement explicitly adds a pinned dependency.
- The Rust implementation will pin its channel/components/targets in `rust-toolchain.toml`.
- `Cargo.lock` is committed for product binaries and repository tools.
- Dependency additions require an owning issue, exact version/feature rationale, license review, security/advisory consideration, and deterministic/offline impact assessment.
- CI action versions and external build tools must be pinned to an auditable version or immutable revision according to the supply-chain policy as it lands.
- A local unpinned tool may assist exploration, but its output is not release evidence.

Supported production platforms and capability tuples are declared only by the release claim manifest from issue #5 and final qualification reports.

## 3. Single quality-command contract

Issue #6 owns the implementation of one repository-level runner. Its stable command contract is:

```bash
python3 tools/quality.py --mode <fast|full|release>
```

Until that runner lands, use:

```bash
python3 tools/validate_baseline.py .
```

Required semantics:

- `fast`: deterministic developer feedback; may omit expensive qualification and can never certify a release.
- `full`: all repository formatting, lint, generation parity, schemas, fixtures, unit/integration tests, documentation checks, and available implementation checks.
- `release`: `full` plus clean-environment, corpus, qualification, security/resource, reproducibility, packaging, and evidence-receipt gates required by the release roadmap.

A mode must fail closed when a required tool, input, test inventory, generated artifact, or evidence receipt is missing. `skipped`, `not run`, stale cache, and empty test discovery are not passes.

## 4. Generated-contract parity

`machine/logical-model.yaml` plus the pinned generator is the single logical-model authority. A change to it must:

1. regenerate every declared generated contract;
2. demonstrate byte-for-byte parity on a second clean generation;
3. update compatible schemas, examples, negative fixtures, traceability, and migration notes;
4. reject hand-edited generated output;
5. record semantic and compatibility impact.

A generated artifact includes a source/generator digest where the baseline specifies one. Locale, timezone, filesystem order, working directory, username, and wall-clock time must not cause normative byte drift.

## 5. Clean, full, and incremental behavior

As caches and incremental implementation appear, the same logical inputs and pinned configuration must produce equivalent authoritative outputs across:

- a clean checkout/build;
- a full rebuild;
- an incremental rebuild;
- a repeated warm-cache run.

Cache metadata and execution timings may differ only where explicitly non-authoritative. Cache corruption, wrong versions, wrong source identities, or missing evidence must be detected; the system must not silently reuse stale results.

## 6. Failure and claim discipline

Every command and API must distinguish complete success from valid partial output, unsupported request, indeterminate result, policy block, resource limit, cancellation, invalid input, and internal failure. No convenience boolean may collapse these states.

A capability is production-qualified only for an exact released product/adapter/dependency/platform/profile/corpus tuple with a passing qualification report. Unit tests, examples, screenshots, schema acceptance, or a successful single document do not establish that claim.

## 7. Commit and CI expectations

- Keep commits bounded and reference the owning issue.
- Run the required quality mode for the change and record exact commands/results.
- CI and local execution must agree for the same revision and declared environment.
- Main-branch integration must preserve prior valid canonical artifacts and fail atomically on generation or validation errors.
- Release evidence must be retained in machine-readable form and bound to the exact source revision/toolchain/dependencies/corpus.

## 8. Generated and binary artifacts

Canonical text sources and small deterministic fixtures belong in Git. Large binaries, review projections, restricted corpora, and qualification bundles use the repository's declared artifact/release storage policy. Their manifests, hashes, provenance, generation recipes, license/privacy classification, and required retrieval instructions remain version-controlled.

Never commit confidential source documents, credentials, or unrestricted exploit samples. Follow `SECURITY.md` and corpus privacy policy.
