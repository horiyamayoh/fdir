
# FDIR 2.1 — Full-fidelity Document Information Representation

FDIR is an intermediate representation and compiler architecture for extracting **recorded information** from documents without making downstream consumers depend on whether the carrier was DOCX, XLSX, PDF, Markdown, or another format.

> The product-facing IR describes **what was recorded**. The evidence substrate describes **where it appeared, what physically carried it, and why a converter made each statement**. Neither side may substitute for the other.

## Baseline status

| Item | Status |
|---|---|
| Normative baseline | `FDIR 2.1.0` |
| Design status | Final and frozen for the 2.1 line |
| Logical authority | `machine/logical-model.yaml` + `tools/generate_contracts.py` |
| Baseline validation | `python3 tools/validate_baseline.py .` |
| Production converter implementation | Not provided by this baseline |
| Qualification claim | None |
| Tracking issue | [#2](https://github.com/horiyamayoh/fdir/issues/2) |

A successful schema validation or example extraction is not a production claim. A capability becomes production-qualified only for an exact format/capability/profile tuple backed by qualification evidence.

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

## Authority order

1. `machine/logical-model.yaml` and pinned `tools/generate_contracts.py` are the core logical authority.
2. Generated contracts are normative only when byte-identical to regeneration.
3. Requirements, acceptance tests, profiles, capability registries, and ADRs are additional normative registries.
4. Markdown specifications explain intent and operating rules.
5. PDF, DOCX, PNG, SVG, reports, indexes, and binary qualification bundles are generated projections or evidence packages, never canonical authority.

## Repository map

| Path | Purpose |
|---|---|
| `spec/` | Normative explanatory specification and generated reference |
| `machine/` | Logical model, profiles, requirements, tests, ADRs, migration, and backlog boundary |
| `schemas/` | Deterministically generated core contracts |
| `tools/` | Contract, traceability, canonicalization, and baseline validators |
| `examples/` | Assertion-first logical examples |
| `fixtures/` | Positive, negative, and canonical vectors |
| `matrices/` | Generated requirement/test traceability and design status |
| `queries/` | Format-neutral query examples over the generated SQLite projection |
| `references/` | Packaging, terminology, and authority notes |
| `diagrams/` | Mermaid diagram sources |

## Validate

```bash
python3 tools/validate_baseline.py .
```

The command checks generated-contract parity, schema structure, examples, negative fixtures, accounting closure, canonical vectors, requirement/test traceability, projection boundaries, and Python syntax using only the standard library.
