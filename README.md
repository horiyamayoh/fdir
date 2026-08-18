# FDIR 2.1 — Full-fidelity Document Information Representation

FDIR is an intermediate representation and compiler architecture for extracting **recorded information** from documents without making downstream consumers depend on whether the carrier was DOCX, XLSX, PDF, Markdown, or another format.

> The product-facing IR describes **what was recorded**. The evidence substrate describes **where it appeared, what physically carried it, and why a converter made each statement**. Neither side may substitute for the other.

## Baseline status

| Item | Status |
|---|---|
| Normative baseline | `FDIR 2.1.0` |
| Design status | Final and frozen for the 2.1 line |
| Logical authority | `machine/logical-model.yaml` + `tools/generate_contracts.py` |
| Repository quality | `python3 tools/quality.py --mode full --cache-policy off .` |
| Implementation boundary | Rust-first product; CPython source/verification oracle; frozen by ADR 0004 and Issue #32 |
| Dependency admission | Exact manifest + evidence lanes + isolation policy; no product runtime dependency currently admitted |
| Release claim scope | Scope revision `1`; four tuples; `development-unqualified` |
| Release traceability | `python3 tools/validate_release_traceability.py --check --self-test .` |
| Production converter implementation | Not provided by this baseline |
| Qualification claim | None |
| Baseline import issue | [#2](https://github.com/horiyamayoh/fdir/issues/2) |
| Product umbrella issue | [#1](https://github.com/horiyamayoh/fdir/issues/1) |
| Completion roadmap | [#4](https://github.com/horiyamayoh/fdir/issues/4) |
| Release scope issue | [#5](https://github.com/horiyamayoh/fdir/issues/5) |
| Quality framework issue | [#6](https://github.com/horiyamayoh/fdir/issues/6) |
| Foundation decision issue | [#32](https://github.com/horiyamayoh/fdir/issues/32) |
| First product implementation issue | [#7](https://github.com/horiyamayoh/fdir/issues/7) |

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
3. Requirements, acceptance tests, profiles, capability registries, ADRs, and the versioned `release/` scope registries are additional normative authorities.
4. `machine/implementation-policy.yaml` governs implementation language, evidence lanes, isolation, dependency admission, and the product-development handoff without changing the language-neutral FDIR 2.1 semantics.
5. Markdown specifications explain intent and operating rules.
6. PDF, DOCX, PNG, SVG, reports, indexes, and binary qualification bundles are generated projections or evidence packages, never canonical authority.

## Repository map

| Path | Purpose |
|---|---|
| `spec/` | Normative explanatory specification and generated reference |
| `machine/` | Logical model, profiles, requirements, tests, ADRs, migration, and backlog boundary |
| `release/` | Claim manifest, end-to-end traceability, deferred scope, blocker policy, scope approvals, and the product-development handoff |
| `schemas/` | Deterministically generated core contracts |
| `tools/` | Contract, traceability, canonicalization, and repository-quality validators |
| `tests/` | Standard-library unit and integration-style repository gate tests |
| `quality/` | Pinned toolchain and required-check, cache, and receipt policy |
| `examples/` | Assertion-first logical examples |
| `fixtures/` | Positive, negative, and canonical vectors |
| `matrices/` | Generated requirement/test, claim, traceability, and design-status projections |
| `queries/` | Format-neutral query examples over the generated SQLite projection |
| `references/` | Packaging, terminology, and authority notes |
| `diagrams/` | Mermaid diagram sources |

## Project policies

- [Contributing and issue lifecycle](CONTRIBUTING.md)
- [Development and build policy](DEVELOPMENT.md)
- [Repository quality and required-check policy](quality/README.md)
- [Product-development handoff](release/development-handoff.md)
- [Dependency candidate assessment baseline](references/dependency-candidate-assessments.md)
- [Security and private vulnerability reporting](SECURITY.md)
- [Apache License 2.0](LICENSE)
- [Issue intake forms](.github/ISSUE_TEMPLATE/)
- [Pull request checklist](.github/pull_request_template.md)

The project owner currently permits small validated commits directly to `main`; external contributions should normally use a focused issue and pull request. Issue and PR templates require ownership, acceptance criteria, evidence, claim impact, and intentionally deferred work.

## Validate

The required local and CI integration command is:

```bash
python3 tools/quality.py --mode full --cache-policy off .
```

It uses only CPython's standard library and checks the pinned toolchain, text format, Python lint, documentation links, the frozen implementation/dependency policy, generated-contract byte parity, JSON Schema/JSON-LD/CDDL/SQLite contract structure, positive and negative fixtures, normative requirement/test traceability, baseline and release-scope traceability, unit tests, CI policy, repository policy, and unsupported production claims. The default machine-readable receipt is `reports/quality/full.json`.

For a shorter edit loop that cannot certify integration or release:

```bash
python3 tools/quality.py --mode fast --cache-policy off .
```

To demonstrate that every major gate rejects an intentional defect:

```bash
python3 tools/quality.py --self-test-gates .
```

Explicit `read-write` and `read-only` cache policies are available for equivalence checks; neither policy skips authoritative gates. `release` mode adds fail-closed qualification and intentionally fails while the claim manifest remains `development-unqualified`. See [the repository quality policy](quality/README.md) for the mode matrix, receipt schemas, cache rules, exact required check name, and release boundary.

The lower-level validators remain available for focused diagnosis:

```bash
python3 tools/validate_baseline.py .
python3 tools/validate_implementation_policy.py --check --self-test --json .
python3 tools/validate_release_traceability.py --check --self-test --json .
```
