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

- Repository quality tooling supports CPython 3.12 and 3.13, pins CPython 3.12 in CI, and uses only the standard library; `.python-version` and `quality/toolchain.json` are the declared prerequisite authority.
- The Rust implementation will pin its channel/components/targets in `rust-toolchain.toml`.
- `Cargo.lock` is committed for product binaries and repository tools.
- Dependency additions require an owning issue, exact version/feature rationale, license review, security/advisory consideration, and deterministic/offline impact assessment.
- CI action versions and external build tools must be pinned to an auditable version or immutable revision according to the supply-chain policy as it lands.
- A local unpinned tool may assist exploration, but its output is not release evidence.

Supported production platforms and capability tuples are declared only by the release claim manifest from issue #5 and final qualification reports.

## 3. Implementation boundary and dependency admission

ADR 0004 and `machine/implementation-policy.yaml` freeze the implementation boundary before product code begins:

- the reference product is Rust-first, while the existing CPython standard-library generators and validators remain an independent source/verification oracle;
- canonical JSON and the published vectors remain the FDIR 2.1 identity authority;
- every dependency output declares one or more of `native-substrate-census`, `semantic-helper`, `renderer-observation`, `ocr-inference-observation`, and `storage-codec`;
- a semantic helper, renderer, OCR engine, or inference backend cannot substitute for native evidence or an independent census;
- unsafe/FFI/native or non-Rust document workers receiving untrusted bytes use an `isolated-worker` boundary by default;
- an in-process exception requires a dedicated accepted ADR, threat analysis, bounded input contract, and exact-build qualification evidence.

No product runtime dependency is currently admitted. A proposal must use `.github/ISSUE_TEMPLATE/dependency.yml`, conform to `machine/dependency-manifest.schema.json`, be recorded at an exact version in `machine/dependency-catalog.yaml`, and remain owned by an implementation issue plus Issue #33. Validate the complete policy with:

```bash
python3 tools/validate_implementation_policy.py --check --self-test --json .
```

Dependency admission is bounded implementation evidence. It does not create an adapter capability or production claim. The authoritative transition into product work is [`release/development-handoff.md`](release/development-handoff.md), whose first implementation issue is #7.

## 4. Single quality-command contract

The implemented repository-level entry point is:

```bash
python3 tools/quality.py --mode <fast|full|release> --cache-policy <off|read-write|read-only> .
```

The canonical integration command is:

```bash
python3 tools/quality.py --mode full --cache-policy off .
```

Required semantics:

- `fast`: deterministic developer feedback over toolchain, formatting, lint, documentation, implementation/dependency policy, generated contracts, schema contracts, fixtures, traceability, baseline validity, and claim discipline; it is not integration or release certification.
- `full`: every `fast` gate plus generated release traceability, release-scope validation, all discovered tests, CI policy, and repository policy. It emits durable integration evidence.
- `release`: every `full` gate plus explicit release qualification. It must fail while any declared tuple remains unqualified or non-production-ready.

Every run writes a machine-readable receipt under `reports/quality/` unless `--receipt` selects another path. A mode fails closed when a required tool, input, test inventory, generated artifact, policy document, cache identity, or evidence condition is missing. `skipped`, `not run`, stale cache, empty test discovery, and an unexpected gate exception are failures, not passes.

The exact gate matrix, required check name, cache rules, receipt fields, and intentional-failure command are normative repository policy in [`quality/README.md`](quality/README.md).

## 5. Generated-contract parity

`machine/logical-model.yaml` plus the pinned generator is the single logical-model authority. A change to it must:

1. regenerate every declared generated contract;
2. demonstrate byte-for-byte parity on a second clean generation;
3. update compatible schemas, examples, negative fixtures, traceability, and migration notes;
4. reject hand-edited generated output;
5. record semantic and compatibility impact.

A generated artifact includes a source/generator digest where the baseline specifies one. Locale, timezone, filesystem order, working directory, username, and wall-clock time must not cause normative byte drift.

## 6. Clean, full, and incremental behavior

The same logical inputs and pinned configuration must produce equivalent authoritative outputs across:

- a clean checkout and `--cache-policy off` run;
- a full `--cache-policy read-write` run;
- an incremental or repeated `--cache-policy read-only` run;
- a repeated warm-cache run.

All three policies execute every authoritative gate. `read-write` creates cache metadata only after a complete pass. `read-only` requires matching schema, quality version, mode, source digest, gate plan, and result digest, then compares that cache with a fresh execution. Cache metadata and normalized timings may differ only where explicitly non-authoritative. Cache corruption, wrong versions, wrong source identities, or missing evidence must be detected; the system must not silently reuse stale results.

## 7. Failure and claim discipline

Every command and API must distinguish complete success from valid partial output, unsupported request, indeterminate result, policy block, resource limit, cancellation, invalid input, and internal failure. No convenience boolean may collapse these states.

A capability is production-qualified only for an exact released product/adapter/dependency/platform/profile/corpus tuple with a passing qualification report. Unit tests, examples, screenshots, schema acceptance, or a successful single document do not establish that claim.

## 8. Commit and CI expectations

- Keep commits bounded and reference the owning issue.
- Run `python3 tools/quality.py --mode full --cache-policy off .` before integration and record the receipt or concise result.
- Pull requests and protected integration into `main` require the exact GitHub Actions check `quality / full`; skipped, neutral, cancelled, or missing runs are not success.
- CI and local execution must agree on success or failure for the same revision, mode, cache policy, and supported Python series.
- Main-branch integration must preserve prior valid canonical artifacts and fail atomically on generation or validation errors.
- CI retains full, cache-equivalence, and intentional-failure receipts for 90 days. Release evidence must remain machine-readable and bound to the exact source revision, toolchain, dependencies, corpus, and approvals.

## 9. Generated and binary artifacts

Canonical text sources and small deterministic fixtures belong in Git. Large binaries, review projections, restricted corpora, and qualification bundles use the repository's declared artifact/release storage policy. Their manifests, hashes, provenance, generation recipes, license/privacy classification, and required retrieval instructions remain version-controlled.

Never commit confidential source documents, credentials, or unrestricted exploit samples. Follow `SECURITY.md` and corpus privacy policy.
