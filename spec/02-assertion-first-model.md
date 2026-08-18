
# 2. Assertion-first recorded-information model

`InformationUnit` carries stable identity and construction only. A unit does not become a paragraph, table, formula, hidden item, or text value merely because a convenience field says so. Those facts are `RecordAssertion` records with status, value, context, confidence when applicable, and occurrence links.

Candidate, rejected, superseded, and unresolved assertions remain visible. `AcceptedProjection` may materialize accepted assertions for efficient consumption, but it must list the contributing assertion identifiers and can always be rebuilt.
