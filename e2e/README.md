# Real-input E2E regression

The standard unittest command runs the complete four-format E2E regression:

```text
python -m unittest discover -s tests -p "test_*.py"
```

The runner creates DOCX, XLSX, PDF, and Markdown fixtures in a temporary
directory, invokes the public converter, validates the IR, canonicalizes it,
and exercises a typed query. For each format it also checks malformed input,
unsupported features, and the configured input-size limit: 16 cases in total.

The generated input files, IR, and product conversion metadata sidecars are
removed when each test finishes. They are never committed or used as a
separate completion record.
