# 8. Product review

Review focuses on whether the implementation records form facts faithfully
within its declared bounded scope.

## Review questions

- Are structure, text, style, layout, geometry, order, and observations
  represented as separate typed facts?
- Are source values, normalized values, displayed values, and observed values
  kept distinct?
- Do unsupported, ambiguous, unavailable, and failed cases remain explicit?
- Are relationships reciprocal and queryable without semantic predicates?
- Are extension payloads typed and versioned without opening the core model?
- Does canonical IR identity depend only on canonical IR content?
- Do negative tests reject fabricated values, missing references, cycles,
  malformed inputs, and resource-limit violations?

## Scope decisions

FDIR does not provide business semantics, semantic equivalence, lineage
certificates, forensic byte archives, or source-byte stores. Those are separate
downstream responsibilities. Product changes that affect these boundaries
must update the schema, documentation, examples, and local regression tests
together.
