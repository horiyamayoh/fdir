
# ADR 0003: Assertion-first kernel

Status: accepted for FDIR 2.1.0

`InformationUnit` is restricted to identity and construction. Unit kind, text, value, visibility, style facets, limitations, and lineage are `RecordAssertion` statements. Consumers may use `AcceptedProjection` as a convenience view, but must be able to recover the contributing assertion identifiers and source occurrences.
