# 7. Product verification

FDIR development is complete when the local product test command succeeds:

```text
python -m unittest discover -s tests -p "test_*.py"
```

The suite covers schema and generated-contract consistency, typed model
invariants, canonical serialization, query/index behavior, all four adapter
families, malformed and unsupported input, explicit partial status,
resource limits, and real-input conversion.

## Test layers

- Unit tests exercise adapters, validators, canonicalization, query helpers,
  extension handling, and stable diagnostics.
- The acceptance matrix checks the 134 product requirements declared in
  `machine/requirements.json` and `machine/acceptance-tests.json`.
- Real-input tests invoke the public converter for DOCX, XLSX, PDF, and
  Markdown, including normal, malformed, unsupported, and bounded-resource
  cases.
- Regression fixtures use literal expected product facts. They do not derive
  expected values from the implementation under test.

## Completion rules

Every changed behavior needs a passing positive or negative test. A feature
that is unsupported or unavailable must produce its declared diagnostic and
status; it must not be silently omitted or converted into a success. Tests
must leave no repository reports or generated files behind.

FDIR remains a form-fact layer. Semantic interpretation, business meaning,
semantic equivalence, and raw-byte archiving belong outside this product.
