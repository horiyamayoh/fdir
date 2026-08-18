# FDIR 2.1 product-development handoff

## Foundation decision

Issue #32 freezes the implementation boundary for the FDIR 2.1 line:

- the reference product is Rust-first;
- the existing CPython standard-library generators and validators remain an independent source/verification oracle;
- canonical JSON and the published vectors remain the 2.1 identity authority;
- dependency output is separated into `native-substrate-census`, `semantic-helper`, `renderer-observation`, `ocr-inference-observation`, and `storage-codec` lanes;
- unsafe/FFI/native or non-Rust document workers receiving untrusted bytes are isolated by default;
- every product dependency requires an exact manifest and executable conformance evidence.

This handoff adds **No product capability** and no production qualification. It establishes the controlled point from which issue-driven implementation begins.

## Start sequence

The first product implementation issue is **Issue #7**. Its bounded outcome is the pinned Rust workspace, acyclic crate architecture, minimal CLI skeleton, developer test harness, and Rust gates integrated through the existing Python quality entry point.

The initial critical path is:

1. Issue #7 — Rust workspace, crate boundaries, toolchain, CLI skeleton, and test harness;
2. Issue #8 — generated foundational domain types and assertion-first logical kernel;
3. Issue #9 — canonical JSON, content digests, and acyclic identity DAG;
4. Issues #10 and #11 — canonical persistence and rebuildable SQLite materialization;
5. Issues #12, #13, and #33 — adapter boundary, exhaustive accounting, and dependency conformance;
6. the format, projection, qualification, packaging, and final-release phases owned by roadmap #4.

Parallel work is permitted only where the owning issues declare compatible prerequisites and path ownership. A later phase may not claim its phase gate while a stated prerequisite remains incomplete.

## Definition of Ready

An implementation issue is ready only when it has:

- one owning roadmap or milestone issue;
- explicit prerequisites and coordinated seams;
- bounded responsibilities and owned paths;
- positive, negative, partial, and failure acceptance criteria appropriate to its scope;
- a declared authority, compatibility, security, resource, and release-claim impact;
- exact dependency candidates or an explicit statement that none are introduced;
- completion evidence that can be produced in the current repository quality framework.

A dependency proposal additionally uses `.github/ISSUE_TEMPLATE/dependency.yml` and supplies a complete record conforming to `machine/dependency-manifest.schema.json`.

## Implementation contract

- Preserve machine authority and generated-contract parity. Do not hand-edit generated normative outputs.
- Keep native evidence, semantic candidates, render observations, OCR/inference observations, and storage/codec responsibilities distinct.
- Do not allow a convenience parser to define its own census or silently shrink an inventory domain.
- Use opaque artifact/object handles and deny network access by default across worker boundaries.
- Keep incomplete, unsupported, unresolved, unreadable, policy-excluded, resource-limited, cancelled, crashed, and failed outcomes explicit.
- Do not expose an incomplete feature as production-ready, even behind a successful demo path.
- Commit small, reviewable changes tied to one owning issue and include exact validation evidence.

## Dependency admission workflow

1. Open a dependency assessment issue linked to its implementation owner and Issue #33.
2. Record exact version/build/features, language, lanes, input/output kinds, all normalization and unavailable distinctions, unsafe/FFI/native facts, untrusted-byte exposure, process boundary, license/advisory status, determinism, network policy, resources, qualification state, and owner issue.
3. Add the exact manifest to `machine/dependency-catalog.yaml` only after the bounded admission review passes.
4. Add positive and intentional-failure conformance evidence. Semantic-helper, renderer, or OCR output must fail when offered as the sole native census.
5. Re-run the policy and full repository gates. Admission does not create a format or production claim.

## Required validation

Focused policy validation:

```bash
python3 tools/validate_implementation_policy.py --check --self-test --json .
```

Mandatory integration validation:

```bash
python3 tools/quality.py --mode full --cache-policy off .
```

Quality-gate changes also require:

```bash
python3 tools/quality.py --self-test-gates .
```

The required pull-request check is `quality / full`. A skipped, neutral, cancelled, empty-test, stale-cache, or unavailable run is not a pass.

## Definition of Done

A bounded implementation issue is complete only when:

- every owned acceptance criterion is satisfied with reproducible evidence;
- required generated outputs and traceability projections are current;
- positive and intentional negative behavior both pass;
- dependency and evidence-lane declarations match actual behavior;
- no owned failure state is hidden or converted to success;
- the exact full quality command passes for the integrated revision;
- deferred work is linked to an owning issue and does not leave an orphan responsibility;
- documentation and release-claim state describe only what is actually implemented and qualified.

Whole-product qualification remains owned by the later roadmap phases and Issue #26. Closing a leaf issue never implies that an adapter, format tuple, or FDIR 2.1 release is production-qualified.
