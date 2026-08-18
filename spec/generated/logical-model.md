# Generated logical-model reference

> Generated from `machine/logical-model.yaml`; do not edit manually.

Model: `https://fdir.dev/model/2.1`
Version: `2.1.0`
Root entity: `Snapshot`

## Enumerations

### AccountingDisposition

`represented`, `residual`, `unsupported`, `unreadable`, `policy-excluded`, `duplicate`

### AssertionStatus

`accepted`, `candidate`, `rejected`, `superseded`, `unresolved`

### CompletenessState

`complete`, `partial`, `unsupported`, `unresolved`, `cancelled`, `failed`

### DiagnosticSeverity

`info`, `warning`, `error`, `fatal`

### EquivalenceOutcome

`equivalent`, `not-equivalent`, `indeterminate`

### RelationKind

`contains`, `precedes`, `references`, `annotates`, `groups`, `derived-from`, `continues`

### UnitKind

`document`, `section`, `paragraph`, `span`, `list`, `list-item`, `table`, `row`, `cell`, `figure`, `formula`, `field`, `note`, `comment`, `custom`

### Visibility

`visible`, `hidden`, `conditional`, `unknown`

## Entities

### Artifact

A source or derived byte sequence with a content digest.

| Property | Type | Required |
|---|---|---|
| `artifactId` | `id` | yes |
| `byteLength` | `integer` | no |
| `digest` | `digest` | yes |
| `mediaType` | `string` | yes |
| `role` | `string` | no |

### Carrier

A format-specific physical or logical carrier within an artifact.

| Property | Type | Required |
|---|---|---|
| `artifactId` | `ref → Artifact` | yes |
| `carrierId` | `id` | yes |
| `carrierType` | `string` | yes |
| `path` | `string` | no |

### Surface

A page, sheet, slide, viewport, or other addressable presentation surface.

| Property | Type | Required |
|---|---|---|
| `carrierId` | `ref → Carrier` | yes |
| `label` | `string` | no |
| `surfaceId` | `id` | yes |
| `surfaceType` | `string` | yes |

### Geometry

Geometry recorded in a named coordinate space without implying semantic truth.

| Property | Type | Required |
|---|---|---|
| `bounds` | `number-array` | yes |
| `coordinateSpace` | `string` | yes |
| `geometryId` | `id` | yes |
| `surfaceId` | `ref → Surface` | yes |

### Selector

A format-specific locator into a carrier or artifact.

| Property | Type | Required |
|---|---|---|
| `selectorId` | `id` | yes |
| `selectorType` | `string` | yes |
| `value` | `object` | yes |

### Occurrence

A located occurrence that links recorded claims to source evidence.

| Property | Type | Required |
|---|---|---|
| `carrierId` | `ref → Carrier` | yes |
| `geometryIds` | `ref-array → Geometry` | no |
| `occurrenceId` | `id` | yes |
| `selectorIds` | `ref-array → Selector` | yes |

### Observation

A measured or inferred observation with method and confidence retained.

| Property | Type | Required |
|---|---|---|
| `confidence` | `number` | no |
| `method` | `string` | yes |
| `observationId` | `id` | yes |
| `occurrenceId` | `ref → Occurrence` | yes |
| `value` | `any` | yes |

### InventoryDomain

A bounded census domain for exhaustive source accounting.

| Property | Type | Required |
|---|---|---|
| `carrierId` | `ref → Carrier` | yes |
| `domainType` | `string` | yes |
| `expectedCount` | `integer` | no |
| `inventoryDomainId` | `id` | yes |

### AccountingItem

Exactly one disposition for one independently inventoried source item.

| Property | Type | Required |
|---|---|---|
| `accountingItemId` | `id` | yes |
| `diagnosticIds` | `ref-array → Diagnostic` | no |
| `disposition` | `enum (AccountingDisposition)` | yes |
| `inventoryDomainId` | `ref → InventoryDomain` | yes |
| `sourceKey` | `string` | yes |
| `unitIds` | `ref-array → InformationUnit` | no |

### IndependentCensusReceipt

An independently produced census result used to close accounting domains.

| Property | Type | Required |
|---|---|---|
| `digest` | `digest` | yes |
| `inventoryDomainId` | `ref → InventoryDomain` | yes |
| `observedCount` | `integer` | yes |
| `receiptId` | `id` | yes |

### InformationUnit

An identity and construction anchor; substantive content is asserted separately.

| Property | Type | Required |
|---|---|---|
| `unitId` | `id` | yes |

### RecordAssertion

The authoritative statement of unit class, facets, value, visibility, lineage, or limitation.

| Property | Type | Required |
|---|---|---|
| `assertionId` | `id` | yes |
| `confidence` | `number` | no |
| `contextId` | `ref → InterpretationContext` | no |
| `occurrenceIds` | `ref-array → Occurrence` | yes |
| `predicate` | `string` | yes |
| `status` | `enum (AssertionStatus)` | yes |
| `unitId` | `ref → InformationUnit` | yes |
| `value` | `any` | yes |

### InformationRelation

A typed relation between information units, expressed as an assertion-like statement.

| Property | Type | Required |
|---|---|---|
| `occurrenceIds` | `ref-array → Occurrence` | no |
| `relationId` | `id` | yes |
| `relationKind` | `enum (RelationKind)` | yes |
| `sourceUnitId` | `ref → InformationUnit` | yes |
| `status` | `enum (AssertionStatus)` | yes |
| `targetUnitId` | `ref → InformationUnit` | yes |

### AcceptedProjection

A convenience view derived only from accepted assertions; never a substitute for assertions.

| Property | Type | Required |
|---|---|---|
| `assertionIds` | `ref-array → RecordAssertion` | yes |
| `projectionId` | `id` | yes |
| `unitId` | `ref → InformationUnit` | yes |
| `value` | `object` | no |

### InterpretationContext

A declared context for conditional visibility or interpretation.

| Property | Type | Required |
|---|---|---|
| `contextId` | `id` | yes |
| `parameters` | `object` | yes |

### GuaranteeStatus

A status vector for one guarantee profile and bounded scope.

| Property | Type | Required |
|---|---|---|
| `diagnosticIds` | `ref-array → Diagnostic` | no |
| `guaranteeStatusId` | `id` | yes |
| `profileId` | `string` | yes |
| `state` | `enum (CompletenessState)` | yes |

### EquivalenceCertificate

A profile-scoped, evidence-backed equivalence decision.

| Property | Type | Required |
|---|---|---|
| `certificateId` | `id` | yes |
| `coverageStatusIds` | `ref-array → GuaranteeStatus` | yes |
| `evidenceDigest` | `digest` | no |
| `leftSnapshotId` | `id` | yes |
| `outcome` | `enum (EquivalenceOutcome)` | yes |
| `profileId` | `string` | yes |
| `rightSnapshotId` | `id` | yes |

### LineageCertificate

A cross-revision continuity claim that is distinct from identity and equivalence.

| Property | Type | Required |
|---|---|---|
| `evidenceDigest` | `digest` | no |
| `lineageCertificateId` | `id` | yes |
| `predecessorUnitIds` | `id-array` | yes |
| `status` | `enum (AssertionStatus)` | yes |
| `successorUnitIds` | `id-array` | yes |

### Diagnostic

A visible limitation, unsupported condition, conflict, or failure.

| Property | Type | Required |
|---|---|---|
| `code` | `string` | yes |
| `diagnosticId` | `id` | yes |
| `message` | `string` | yes |
| `relatedIds` | `id-array` | no |
| `severity` | `enum (DiagnosticSeverity)` | yes |

### Snapshot

A canonical FDIR snapshot containing recorded information and its evidence substrate.

| Property | Type | Required |
|---|---|---|
| `acceptedProjections` | `entity-array → AcceptedProjection` | no |
| `accountingItems` | `entity-array → AccountingItem` | yes |
| `artifacts` | `entity-array → Artifact` | yes |
| `assertions` | `entity-array → RecordAssertion` | yes |
| `carriers` | `entity-array → Carrier` | yes |
| `censusReceipts` | `entity-array → IndependentCensusReceipt` | no |
| `claims` | `object` | no |
| `diagnostics` | `entity-array → Diagnostic` | yes |
| `equivalenceCertificates` | `entity-array → EquivalenceCertificate` | no |
| `fdirVersion` | `const` | yes |
| `geometries` | `entity-array → Geometry` | no |
| `guaranteeStatuses` | `entity-array → GuaranteeStatus` | yes |
| `interpretationContexts` | `entity-array → InterpretationContext` | no |
| `inventoryDomains` | `entity-array → InventoryDomain` | yes |
| `lineageCertificates` | `entity-array → LineageCertificate` | no |
| `observations` | `entity-array → Observation` | no |
| `occurrences` | `entity-array → Occurrence` | yes |
| `relations` | `entity-array → InformationRelation` | yes |
| `selectors` | `entity-array → Selector` | yes |
| `snapshotId` | `id` | yes |
| `surfaces` | `entity-array → Surface` | no |
| `units` | `entity-array → InformationUnit` | yes |
