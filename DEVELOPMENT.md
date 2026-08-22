# Development

FDIR changes are product changes to the typed Document Form IR, its bounded
adapters, query surface, or diagnostics. The schema and product requirements
are the source of truth; generated contracts must remain reproducible from
those sources.

## Validation

The only development completion check is:

```powershell
python -m unittest discover -s tests -p "test_*.py"
```

The suite includes the 134 declared acceptance cases, adapter and invariant
tests, query/index tests, real-input E2E across DOCX/XLSX/PDF/Markdown, and
malformed, unsupported, partial, and resource-limit cases. A test must assert
the expected product behavior; unsupported behavior is represented as an
explicit diagnostic rather than skipped.

Tests use temporary directories and must not write reports, manifests, or
other generated products into the repository.

## Change checklist

1. Update the schema, requirements, examples, or adapter together when the
   product contract changes.
2. Add or update positive and negative tests for the changed behavior.
3. Keep source facts, normalized values, displayed values, observations, and
   unsupported states distinct.
4. Run the complete unittest command from the repository root.
5. Confirm the working tree contains no test-generated files.

FDIR does not infer semantic meaning, semantic equivalence, business truth,
or source-byte archives. Core objects remain typed and closed; format-specific
facts belong in registered extensions.
