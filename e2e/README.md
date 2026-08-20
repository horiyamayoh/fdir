# Real-input E2E

Run the complete real-document qualification from the repository root:

~~~text
python tools/run_e2e.py --all
python tools/run_e2e.py --all --json
python tools/run_e2e.py --all --keep e2e/.run/manual
~~~

The runner generates deterministic DOCX, XLSX, PDF, and Markdown files with
`tools/generate_e2e_fixtures.py`. It then invokes the public converter in
child processes. Each valid format case executes `inspect`, `convert`, IR
validation, canonicalization, and a typed query, and verifies execution
evidence plus source-derived content. The suite also runs one malformed-input,
one unsupported-feature/partial-conversion, and one input-size-limit case for
each format: 16 real-input cases in total.

Expected source-derived checks are:

- DOCX: `FDIR DOCX E2E` and `bold`
- XLSX: `Alpha` and `SUM(B2:B3)`
- PDF: `FDIR PDF E2E`
- Markdown: `FDIR Markdown E2E`, `bold`, and `authoring-facts`

Generated source files, IR, evidence sidecars, and JSON reports live under
the ignored `e2e/.run/` directory. No pre-authored IR can satisfy this gate;
the report records the inspected input path, size, SHA-256, adapter outcome,
whether the parser was attempted (and whether a limit rejected it first),
diagnostics, canonical digest, and query result.
