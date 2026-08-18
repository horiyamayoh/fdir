# Requirement / acceptance-test traceability

> Generated; do not edit manually.

| Requirement | Level | Acceptance tests | Text |
|---|---|---|---|
| `FDIR-AUTH-001` | must | `AT-AUTH-PARITY` | machine/logical-model.yaml and the pinned contract generator jointly define the core logical contract. |
| `FDIR-AUTH-002` | must | `AT-AUTH-PARITY` | Generated contracts are normative only when byte-identical to deterministic regeneration. |
| `FDIR-AUTH-003` | must | `AT-PROJECTION-BOUNDARY` | Generated review projections and binary qualification artifacts are non-canonical. |
| `FDIR-MODEL-001` | must | `AT-ASSERTION-FIRST` | InformationUnit is an identity and construction anchor, not a substantive-content container. |
| `FDIR-MODEL-002` | must | `AT-ASSERTION-FIRST` | Substantive recorded information is expressed by RecordAssertion with explicit status and evidence links. |
| `FDIR-MODEL-003` | must | `AT-ASSERTION-FIRST` | AcceptedProjection is derivable exclusively from accepted assertions and cannot replace them. |
| `FDIR-EVID-001` | must | `AT-EVIDENCE-LINKS` | Evidence entities remain separate from recorded-information entities. |
| `FDIR-EVID-002` | must | `AT-EVIDENCE-LINKS` | Every accepted recorded assertion retains at least one source occurrence unless a diagnostic explains the absence. |
| `FDIR-EVID-003` | must | `AT-EVIDENCE-LINKS` | Format-specific vocabulary remains in carriers, selectors, occurrences, observations, and adapters. |
| `FDIR-ACCT-001` | must | `AT-ACCOUNTING-CLOSURE`, `AT-ACCOUNTING-DUPLICATE` | Every independently inventoried source item receives exactly one accounting disposition. |
| `FDIR-ACCT-002` | must | `AT-ACCOUNTING-CLOSURE` | Accounting closure is checked against an independently produced census receipt where the profile requires it. |
| `FDIR-ACCT-003` | must | `AT-ACCOUNTING-CLOSURE`, `AT-VISIBLE-UNSUPPORTED` | Unsupported, unreadable, residual, and policy-excluded items remain visible. |
| `FDIR-COMP-001` | must | `AT-COMPLETENESS-VECTOR` | Completeness is represented as a guarantee-profile status vector, not one boolean. |
| `FDIR-COMP-002` | must | `AT-VISIBLE-UNSUPPORTED` | Partial, unsupported, unresolved, cancelled, and failed states are not rewritten as success. |
| `FDIR-EQ-001` | must | `AT-EQUIVALENCE-INDETERMINATE` | Equivalence decisions are profile-scoped and evidence-backed. |
| `FDIR-EQ-002` | must | `AT-EQUIVALENCE-INDETERMINATE`, `AT-EQUIVALENCE-REJECT` | Insufficient coverage yields indeterminate and cannot yield equivalent. |
| `FDIR-ID-001` | must | `AT-CANONICAL-VECTOR` | Canonical serialization is deterministic and content digests are computed over canonical bytes. |
| `FDIR-ID-002` | must | `AT-IDENTITY-SEPARATION` | Unit identity, cross-format equivalence, and cross-revision continuity remain distinct. |
| `FDIR-VAL-001` | must | `AT-VALIDATION-ENTRY` | The repository exposes one deterministic standard-library-only quality command with fast, full, and fail-closed release modes, explicit cache policies, and machine-readable receipts. |
| `FDIR-VAL-002` | must | `AT-POSITIVE-NEGATIVE` | Positive examples validate and registered negative fixtures fail for their expected reason. |
| `FDIR-VAL-003` | must | `AT-TRACEABILITY` | Every normative requirement is linked to at least one executable acceptance test. |
| `FDIR-CLAIM-001` | must | `AT-NO-PRODUCTION-CLAIM` | The normative baseline does not claim that production converters are implemented or qualified. |
| `FDIR-PKG-001` | must | `AT-PROJECTION-BOUNDARY` | Packaging notes distinguish canonical source from generated PDF, DOCX, PNG, indexes, reports, and binary qualification artifacts. |
