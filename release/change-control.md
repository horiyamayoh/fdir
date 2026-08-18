# FDIR 2.1 release-scope and semantic change control

This policy governs the production claim manifest for the `2.1.x` release line. It does not replace the logical-model authority or weaken the frozen FDIR 2.1 semantics.

## Authority

`release/claim-manifest.yaml`, `release/traceability.yaml`, and `release/deferred-capabilities.yaml` are the version-controlled release-scope authority. Generated matrices are review projections and are valid only when byte-identical to regeneration.

The claim manifest begins in `development-unqualified` state. No tuple or release-wide function is production-ready until the final release gate issues qualification evidence for the exact product, source revision, adapter and dependency versions, platform, profile, interpretation context, corpus, resource/security policy, and report identity.

## Change classes

1. **Editorial clarification** may improve wording without changing tuple membership, scope items, required guarantees, ownership, or qualification obligations. It still requires an auditable commit and validation.
2. **Ownership or evidence routing** may replace an issue only when the replacement explicitly accepts the full responsibility, roadmap #4 and traceability are updated, and no evidence class becomes unowned.
3. **Claim narrowing** may remove or constrain a tuple or scope item to address evidence, security, resource, licensing, or compatibility limits. It requires project-owner approval, a `scopeRevision` increment, an auditable manifest diff, documentation/support updates, and qualification impact review.
4. **Claim expansion** adds a format, capability, profile, scope item, platform, context, or guarantee. It requires a new owning issue, requirement/test/fixture/qualification ownership, project-owner approval, a `scopeRevision` increment, and successful qualification before production wording changes.
5. **Normative semantic change** that weakens assertion-first authority, evidence separation, accounting closure, status vectors, equivalence coverage, canonical identity, or claim discipline is forbidden in `2.1.x`. A required incompatible semantic change belongs to a new major version and migration plan.

## Approval record

The initial development scope is established by issue #5 under umbrella #1 and roadmap #4. Every later semantic scope change must cite a project-owner-approved issue or signed release-scope amendment. The validating tool requires an incremented scope revision and consistent revisions across all release-scope registries; human review verifies the approval evidence.

## Release blockers

The following are release-blocking for any affected tuple:

- unowned normative requirement, acceptance test, schema/ADR obligation, scope item, or evidence class;
- missing, stale, contradictory, or untraceable implementation/test/qualification evidence;
- unresolved accounting gap or silent omission;
- incomplete, unsupported, indeterminate, policy-blocked, resource-limited, cancelled, invalid, or failed required behavior;
- canonical identity or deterministic output divergence;
- critical/high security finding, unmitigated release-blocking medium risk, privacy leak, or unsafe external access;
- unresolved license conflict, unreproducible package, invalid SBOM/provenance/checksum/signature, or unsupported installation path;
- production wording that exceeds the exact qualified tuple.

A blocker may narrow the published claim manifest, but no waiver may relabel missing evidence or failure as success or contradict the FDIR 2.1 normative baseline.

## Deferred capabilities

Deferred capabilities remain outside production claims until a future approved scope revision or major-version roadmap gives them explicit implementation and qualification ownership. Their presence in a design note, parser dependency, experiment, or evidence inventory does not make them supported.
