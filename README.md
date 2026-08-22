# FDIR — Document Form IR

FDIR converts recorded document-form facts from DOCX, XLSX, PDF, and Markdown
into a typed, validated Document Form IR. It preserves structure, text,
styles, layout, geometry, display values, authoring facts, diagnostics, and
explicit unsupported or partial states. It does not infer business meaning
or semantic equivalence.

## Product boundary

The normative product sources are:

- `schemas/document-form-ir.schema.json`
- `machine/requirements.json`
- `machine/acceptance-tests.json`
- `machine/canonicalization.json`
- `machine/capability-profile.json`
- `machine/extension-registry.json`
- `docs/01-product-definition.md` through `docs/06-interfaces-and-implementation.md`

Generated model and query contracts are checked by the local test suite. They
are projections of the product schema, not separate product authorities.

## Local development check

Install the test dependencies when the environment does not already provide
them:

```powershell
python -m pip install --requirement requirements-test.txt
```

Run the complete product suite:

```powershell
python -m unittest discover -s tests -p "test_*.py"
```

The command runs the schema and acceptance matrix, adapter unit tests, query
and index tests, four-format real-input E2E cases, malformed/unsupported and
resource-limit cases, and the checked-in regression corpus. Generated files
are temporary and are removed when each test finishes.

## Converter CLI

```text
python tools/convert_document.py inspect <input> [--format <kind>] [--profile <id>]
python tools/convert_document.py convert <input> --out document-form.json [--format <kind>] [--profile <id>] [--evidence execution.json]
python tools/convert_document.py validate document-form.json
```

The optional `--evidence` output is product ingestion metadata. It records
input and adapter facts outside the IR; it is not required for development
completion.

## Product limitations

Adapter support is bounded and explicit. Unsupported, unavailable,
approximated, ambiguous, or failed features remain diagnostics and residual
states. FDIR records form facts; downstream applications may interpret those
facts, but cannot redefine the IR authority. Renderer and OCR observations do
not replace source-declared facts.

## License and security

See [LICENSE](LICENSE), [NOTICE](NOTICE), [SECURITY.md](SECURITY.md), and
[CONTRIBUTING.md](CONTRIBUTING.md).
