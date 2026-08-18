# ADR 0004: Rust-first reference product, CPython verification oracle, and evidence-lane boundaries

- **Status:** Accepted
- **Date:** 2026-08-19
- **Decision owner:** Issue #32
- **Applies to:** FDIR 2.1.x implementation and qualification
- **Normative semantic impact:** None. The language-neutral FDIR 2.1 logical contract remains frozen.

## Context

The FDIR 2.1 baseline defines recorded information, evidence, exhaustive accounting, status vectors, canonical identity, projection, equivalence, lineage, and qualification without prescribing one implementation language. Product development now needs a stable boundary before Issue #7 creates the Rust workspace and before any parser, renderer, OCR engine, evaluator, or codec becomes an architectural commitment.

Without a frozen boundary, a high-level parser could accidentally become source authority, normalized text or cells could replace native evidence, and each format issue could reopen the same Rust-versus-Python and in-process-versus-worker decisions. The existing CPython standard-library tools already provide an independent contract generator, canonical-vector oracle, baseline validator, release-traceability validator, and repository quality runner. Rewriting those tools merely for language uniformity would remove an independent oracle without improving the FDIR contract.

FDIR 2.1 canonical identity is based on **canonical JSON** and the published canonical vectors. Canonical CBOR is not the current authority and cannot replace canonical JSON without an approved normative version change.

## Decision

### 1. Product and verification boundary

The reference product is **Rust-first**. Issue #7 owns the exact workspace and acyclic crate graph, but the following product responsibilities are implemented in Rust unless a later approved issue records a narrower exception:

- generated domain types and the neutral logical kernel;
- canonical JSON, digests, and identity computation;
- canonical snapshots, content-addressed objects, and rebuildable SQLite materialization;
- adapter protocol types, first-party adapter SDK, coordinator, CLI, and public product API;
- first-party adapters, projection, equivalence, alignment, and lineage.

The existing **CPython standard-library** implementation remains the supported source and verification oracle for contract generation, baseline validation, release traceability, canonical vectors, and repository quality orchestration. It is not an undeclared production converter dependency and is not rewritten solely for language uniformity.

Implementation language never grants authority. Authority follows FDIR entities, source selectors, evidence links, independent census receipts, accounting closure, canonical bytes, explicit versions, and qualification evidence.

### 2. Language-neutral adapter protocol

The adapter protocol is implementation-language-neutral. Rust and non-Rust workers must satisfy the same version negotiation, identity binding, lane separation, status, cancellation, crash, timeout, and resource-limit rules. Issue #12 owns the strict process and resource boundary. Issue #33 owns executable dependency and normalization-loss conformance.

### 3. Evidence lanes

Every product dependency or worker that emits document-derived output declares one or more of these lanes:

| Lane | Permitted role | Forbidden substitution |
|---|---|---|
| `native-substrate-census` | Exact bytes, native records/items, reproducible selectors, inventories, and independent census receipts | Does not by itself create accepted recorded-information assertions |
| `semantic-helper` | Parsed structure or value interpretations as assertion candidates linked to native evidence | Cannot satisfy native evidence or independent census by itself |
| `renderer-observation` | Measured presentation observations with renderer, version, context, and source provenance | Cannot rewrite stored source or impersonate native structure |
| `ocr-inference-observation` | Probabilistic OCR or higher-level candidates with method, confidence, limitations, and provenance | Cannot overwrite native extraction, tags, or accepted assertions |
| `storage-codec` | Deterministic hashing, encoding, compression, persistence, transport, and query support | Has no document-interpretation authority |

A library that returns clean text, cells, paragraphs, objects, or an AST may be useful in the `semantic-helper` lane. That output is never sufficient as the sole source selector, native evidence, independent census, or proof of exhaustive accounting. A helper substitution or upgrade may change candidates, but it may not silently shrink a native inventory domain or change canonical evidence identity.

### 4. Isolation and trust

A dependency that receives untrusted document bytes through unsafe code, FFI, or native code runs as an `isolated-worker` by default. A non-Rust parser, renderer, OCR engine, or evaluator receiving untrusted document bytes also runs behind the Issue #12 boundary.

An in-process exception requires all of the following:

1. a dedicated accepted ADR;
2. a threat analysis;
3. a bounded input contract;
4. qualification evidence for the exact dependency build and configuration.

Workers receive opaque artifact or object handles rather than arbitrary host paths. Ambient credentials, undeclared network access, and arbitrary command execution are forbidden. The default network policy is deny.

### 5. Dependency admission

`machine/implementation-policy.yaml` is the machine-readable decision. `machine/dependency-manifest.schema.json` defines the required dependency/worker record, and `machine/dependency-catalog.yaml` is the admitted product dependency catalog.

Every admitted dependency records an exact version, features, implementation language, evidence lanes, normalizations, unavailable source distinctions, unsafe/FFI facts, untrusted-byte exposure, process boundary, license, advisory snapshot, determinism, network policy, resource characteristics, qualification state, and owning issue. Floating versions are forbidden. An unqualified dependency cannot be marked production-ready.

At this foundation point, **no product runtime dependency is admitted**. Candidate classes are assessed in `references/dependency-candidate-assessments.md`; exact crate or engine selection remains owned by Issues #7, #12, #33, and the format implementation issues.

## Alternatives considered

### All-Python product

Rejected as the default reference-product strategy. It would weaken the intended compiled, strongly typed core and packaging model, while providing no semantic authority benefit. Python remains valuable as an independent verification oracle and may be used by an isolated qualified worker where justified.

### All-Rust with no external engines

Rejected as an absolute requirement. Pure Rust is preferred when it improves safety, determinism, source-addressable access, and packaging, but format fidelity and renderer/OCR qualification may justify external engines. Such engines remain replaceable, explicitly manifested workers rather than trusted-core shortcuts.

### High-level-parser-first design

Rejected. Clean text, paragraphs, cells, ASTs, or repaired object models can normalize away source distinctions before FDIR inventories and selectors exist. High-level libraries therefore remain semantic helpers or differential oracles above an independently grounded native substrate.

## Consequences

- Issue #7 can create the Rust workspace without reopening language authority or runtime-boundary decisions.
- Issue #33 can qualify dependencies against one stable manifest and lane vocabulary.
- Format adapters must build native inventories independently of convenience parser output.
- External engines remain replaceable and cannot silently become source authority.
- The Python and Rust implementations intentionally provide independent parity evidence.
- This decision creates no product implementation or production qualification claim.

## Verification

The frozen decision is checked by:

```bash
python3 tools/validate_implementation_policy.py --check --self-test --json .
python3 tools/quality.py --mode full --cache-policy off .
```

The validator rejects unknown lanes, floating versions, unsafe in-process parsing of untrusted bytes, non-Rust in-process document workers, semantic-helper claims to native authority or census, stale canonical-CBOR wording, missing Issue #32 ownership, and incomplete dependency records.
