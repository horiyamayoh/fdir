# FDIR 2.1 — Full-fidelity Document Information Representation

FDIR is an intermediate representation and compiler architecture for extracting **recorded information** from documents without forcing downstream consumers to depend on whether the source was DOCX, XLSX, PDF, Markdown, or another carrier format.

> The product-facing IR describes **what was recorded**. The evidence substrate describes **where it appeared, what physically carried it, and why a converter made each statement**. Neither side may substitute for the other.

## Development status

| Item | Status |
|---|---|
| Normative baseline | `FDIR 2.1.0` |
| Design status | Frozen for the 2.1 line |
| Machine-contract status | Closed, generated, and self-validating |
| Production converter implementation | In development |
| Umbrella issue | [#1 — FDIR 2.1 production implementation and qualification](https://github.com/horiyamayoh/fdir/issues/1) |
| Integration policy | Small, validated commits directly to `main` |

A successful schema validation or demo extraction is not a production claim. A capability becomes production-qualified only for an exact format/capability/profile tuple backed by the required evidence.

## Decisive model

```text
Recorded-information axis                 Evidence axis
─────────────────────────                 ─────────────
InformationUnit                           Artifact
InformationRelation                       Carrier
RecordAssertion  <--------------------->  Selector / Occurrence
AcceptedProjection                        Surface / Geometry / Observation
EquivalenceCertificate                    InventoryDomain / AccountingItem
```

`InformationUnit` is deliberately an identity/construction anchor. Unit class, facet values, relations, visibility, lineage, and limitations are statements in `RecordAssertion`. This prevents OCR, heuristics, or conflicting candidates from appearing authoritative merely because a consumer reads a convenient object.

## Non-negotiable principles

- Evidence model and recorded-information model are separate and both are required.
- Format-specific vocabulary belongs in carriers, selectors, occurrences, adapters, and evidence—not in the neutral core vocabulary.
- Every independently inventoried source item receives exactly one accounting disposition; silent omission is invalid.
- Completeness is a guarantee-profile status vector, not one boolean.
- Cross-format equivalence is profile-scoped and evidence-backed; insufficient coverage yields `INDETERMINATE`, never `EQUIVALENT`.
- Canonical authority, content-addressed evidence, and rebuildable projections/indexes are distinct.
- Unit identity, cross-format equivalence, and cross-revision continuity are distinct concepts.
- Unsupported, unresolved, partial, cancelled, and failed states must remain visible.

## Authority order

1. `machine/logical-model.yaml` and pinned `tools/generate_contracts.py` are the single source of truth for the core logical contract.
2. Generated schemas, CDDL, SQLite DDL, core vocabulary, JSON-LD context, and generated human references are normative only when byte-identical to regeneration.
3. `machine/requirements.yaml`, `machine/acceptance-tests.yaml`, profiles, capability registries, and ADRs are additional normative registries.
4. Markdown specifications explain intent and operating rules.
5. DOCX/PDF and rendered diagrams are generated review projections.
6. SQLite/search/JSON-LD/rendered reports are rebuildable projections, never canonical authority.

## Planned repository map

| Path | Purpose |
|---|---|
| `spec/` | Normative explanatory specification and generated references |
| `machine/` | Logical model, profiles, requirements, tests, ADRs, migration, and implementation backlog |
| `schemas/` | Generated core contracts and strict auxiliary schemas |
| `tools/` | Contract generator, canonical encoding/identity helpers, and validators |
| `examples/` | Assertion-first logical examples |
| `fixtures/` | Canonical vectors, positive/negative contracts, cross-format corpus, security and qualification inventories |
| `matrices/` | Traceability, coverage, migration, and separate design/implementation scorecards |
| `queries/` | Format-neutral query examples |
| `crates/` | Rust product implementation |

## Initial quality command

Once the normative baseline commit lands:

```bash
python3 tools/validate_baseline.py .
```

The production workspace will add a single repository-level quality command covering generated-contract parity, formatting, linting, unit/integration tests, negative fixtures, and qualification receipts.

## Scope of “final”

“Final” freezes the 2.1 product philosophy, authority hierarchy, logical contracts, identity rules, profiles, release claim rules, and migration boundary. It does not claim that converters already exist, and it does not prohibit additive registered extensions or a future major version. It prohibits quietly changing semantics inside `2.1.x`.
