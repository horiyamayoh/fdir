# Dependency candidate assessment baseline

This document records the **assessment method and representative dependency classes** for FDIR 2.1 product development. It does not select a final crate, parser, renderer, OCR engine, evaluator, model, or native library unless an issue-specific admission section below says otherwise. Exact selections become usable only after admission through `machine/dependency-catalog.yaml`.

The frozen foundation statement was **No product runtime dependency is admitted**. Issue-specific implementation work may supersede that starting state by admitting an exact dependency without creating a production-capability claim; Issue #11 makes the first such bounded admission below.

## Admission decision rule

A candidate is reviewed against the machine-readable manifest in `machine/dependency-manifest.schema.json` and the frozen policy in `machine/implementation-policy.yaml`. Reviewers must establish all of the following before admission:

1. an exact immutable version, build, revision, digest, and feature set;
2. one or more explicit evidence lanes;
3. the precise input/output boundary and every normalization or unavailable source distinction;
4. whether unsafe code, FFI, native code, or untrusted document bytes are involved;
5. the required process boundary, network policy, and resource limits;
6. license status, advisory snapshot, determinism claim, and qualification state;
7. positive, negative, substitution, and normalization-loss evidence owned by an issue.

Clean text, paragraphs, cells, object models, repaired structures, ASTs, rendered pixels, or OCR tokens are useful only in their declared lanes. They do not become native evidence merely because a library presents them conveniently.

## Evidence-lane interpretation

| Lane | Candidate may contribute | Candidate may not substitute for |
|---|---|---|
| `native-substrate-census` | Exact bytes, package/object/token records, selectors, inventory domains, independently checkable counts | Accepted semantic assertions by itself |
| `semantic-helper` | Parsed structure/value candidates linked to native selectors and evidence | Native evidence, independent census, exhaustive accounting, or canonical source identity |
| `renderer-observation` | Versioned surfaces, geometry, marks, and visual measurements | Stored source, native structure, or accepted assertions without explicit interpretation |
| `ocr-inference-observation` | Probabilistic tokens, alternatives, confidence, limitations, and inferred relations | Native text/operators/tags or authoritative consensus |
| `storage-codec` | Deterministic serialization, hashing, compression, persistence, transport, and queries | Document interpretation authority |

## Representative candidate classes

### Rust workspace, serialization, hashing, and storage crates

- **Potential lanes:** normally `storage-codec`; a purpose-built source reader may additionally seek `native-substrate-census` admission.
- **Default boundary:** `trusted-core` or `in-process` only when the crate does not receive untrusted document bytes through unsafe/FFI/native code and its input contract is bounded.
- **Required evidence:** exact version/features, canonical-vector parity, deterministic output, no silent identity normalization, license/advisory review, and dependency graph review.
- **Decision owner:** Issues #7, #9, #10, and #11 according to responsibility.
- **Foundation decision:** viable category; exact selections require issue-owned admission.

### High-level Markdown, DOCX, XLSX, or PDF libraries

- **Potential lane:** `semantic-helper` or differential oracle.
- **Default boundary:** isolated when non-Rust or unsafe/native code receives untrusted bytes; otherwise still subject to the Issue #12 protocol and Issue #33 conformance decision.
- **Known risk:** clean ASTs, paragraphs, cells, text runs, repaired object models, or flattened values may erase byte spelling, trivia, package records, revisions, formulas, fields, object/operator distinctions, unsupported material, and parser recovery facts.
- **Required evidence:** native census remains independently grounded; every helper node links to source selectors; substitution cannot shrink the native inventory; all normalization is declared and tested.
- **Foundation decision:** unsuitable as the sole `native-substrate-census` authority; potentially useful as a qualified `semantic-helper`.

### ZIP, XML, OPC, and source-addressable syntax substrates

- **Potential lane:** `native-substrate-census` when exact records/events/selectors and independent counts remain inspectable.
- **Default boundary:** isolated when unsafe/FFI/native code receives untrusted archives or XML; resource and decompression budgets are mandatory.
- **Required evidence:** duplicate records/names, local-versus-central disagreement, extra fields, encryption/compression variants, namespace/prefix distinctions, unknown material, comments, processing instructions, malformed/recovered regions, and trailing bytes remain visible.
- **Decision owner:** Issues #34 and #33, with DOCX/XLSX integration owned by #15 and #16.
- **Foundation decision:** necessary candidate class; exact implementation remains open.

### Native PDF byte, revision, object, stream, and operator readers

- **Potential lane:** `native-substrate-census` only when original bytes, revisions, xrefs, object redefinitions, streams, filters, operator sequences, recovery facts, and selectors remain distinguishable.
- **Default boundary:** isolated for unsafe/native/non-Rust implementations receiving untrusted PDF bytes.
- **Required evidence:** repaired or decoded structure cannot overwrite original facts; high-level text extraction cannot become the census; incremental revisions and unsupported/encrypted material remain explicit.
- **Decision owner:** Issues #37 and #33.
- **Foundation decision:** necessary candidate class; no engine or library selected.

### Renderers and office/PDF layout engines

- **Potential lane:** `renderer-observation`.
- **Required boundary:** `isolated-worker` when external, native, unsafe, or non-Rust; opaque artifact handles, deny-by-default network, bounded temporary data, cancellation, timeout, and crash semantics are mandatory.
- **Required evidence:** exact engine/build, platform, fonts/resources, configuration, context, output digest, determinism limits, and disagreement with native evidence.
- **Decision owner:** Issues #12, #38, #23, and the relevant format milestone.
- **Foundation decision:** replaceable observation backend only; never source authority.

### OCR and visual-inference engines or models

- **Potential lane:** `ocr-inference-observation`.
- **Required boundary:** `isolated-worker` with explicit model/data identity, offline policy, resource limits, privacy controls, and bounded nondeterminism.
- **Required evidence:** alternatives, confidence, method, limitations, source page/surface identity, and disagreement with tags/native extraction/render observations remain inspectable.
- **Decision owner:** Issues #38, #33, and #23.
- **Foundation decision:** replaceable probabilistic observation backend only; never native census or silent authoritative text.

### Formula evaluators and external-data engines

- **Potential lane:** normally `semantic-helper`; evaluated/displayed values remain separate from stored formula text, cached values, and raw cell evidence.
- **Required boundary:** isolated if non-Rust, native, unsafe, externally resolving, or otherwise exposed to untrusted workbook content.
- **Required evidence:** calculation engine/version, locale/timezone/date system, calculation mode, external-resource policy, nondeterminism, stale-cache behavior, and resource limits.
- **Decision owner:** Issues #16, #12, and #33.
- **Foundation decision:** optional candidate class; unrestricted spreadsheet recalculation is not assumed.

## Issue #11 admission: `rusqlite` 0.40.1 with bundled SQLite

Issue #11 admits exactly `rusqlite` 0.40.1 with only the `bundled` feature for the rebuildable SQLite materialization boundary. The bundled release contains SQLite 3.53.2. The Cargo requirement is exact (`=0.40.1`), and `Cargo.lock` fixes the complete transitive graph.

- **Lane and authority:** `storage-codec` only. SQLite never becomes snapshot, evidence, assertion, provenance, completeness, or identity authority. The index stores a digest-bound canonical snapshot copy and exact source paths solely to validate the derived rows.
- **Input boundary:** generated DDL, canonical snapshot JSON already accepted by `fdir-storage`, and bounded query parameters. It does not receive original untrusted document bytes.
- **Isolation:** `trusted-core` is permitted because the unsafe/FFI/native dependency sees only the bounded storage representation, not document bytes. Runtime network policy is deny.
- **Normalization-loss review:** SQLite INTEGER affinity is used only for generated lengths and operational counters. Every release-relevant record remains exact canonical JSON, and every convenience row links to an RFC 6901 canonical source path. Deleting the database and rebuilding restores the supported query rows.
- **Positive evidence:** clean, full, incremental, and delete/rebuild paths produce identical projection digests and supported query rows.
- **Negative and substitution evidence:** corrupt, incomplete, wrong-application, wrong-version, wrong-DDL, stale-snapshot, and logically divergent databases are rejected. SQLite rows cannot substitute for the canonical snapshot digest or canonical traversal checks.
- **License and security:** upstream is MIT licensed. The Issue #11 admission is development-unqualified; the advisory snapshot remains pending and therefore creates no production qualification.
- **Qualification:** `admitted-unqualified`, owned by Issue #11. Issue #23 and Issue #24 retain security and supply-chain qualification ownership.

## Review outcomes

A review records one of these outcomes without altering the release claim:

- **investigate** — evidence is incomplete; the candidate remains outside the catalog;
- **admit unqualified** — the exact manifest is accepted for bounded implementation work but creates no adapter or production qualification claim;
- **adapter-qualified** — conformance is demonstrated for an exact adapter/capability boundary, still without release-wide qualification;
- **production-qualified** — permitted only with exact release evidence, accepted license/advisory status, and all applicable qualification gates;
- **reject** — incompatible authority, normalization, security, determinism, licensing, resource, or support behavior.

Issue #33 owns executable dependency and normalization-loss conformance. Format issues own final candidate selection and format-specific evidence. Issue #23 owns independent security/privacy qualification. Issue #26 alone can bind a production-qualified dependency stack to a released claim tuple.
