# Contributing to FDIR

Keep changes within the Document Form IR product boundary: recorded form
facts, typed relationships, layout and geometry, display/value lanes,
diagnostics, query behavior, and bounded adapter support.

Before submitting a change:

```powershell
python -m unittest discover -s tests -p "test_*.py"
```

Add regression coverage for every changed behavior, including malformed,
partial, unsupported, or unavailable outcomes where applicable. Do not add
business semantics, semantic-equivalence assertions, raw-byte archives, or
untyped property bags to the core model.

The optional converter `--evidence` sidecar is product ingestion metadata. It
does not form part of the development completion rule.
