# Product regression corpus

These cases are hand-authored source documents kept outside the generated E2E
fixture builder. The corpus is intentionally small and format-diverse: its
purpose is to catch authority, relationship, status, canonicalization, and
query regressions with source material that the ordinary acceptance generator
does not create.

The product regression tests package the OOXML part directories, invoke the
public converter, validate the resulting IR, check source-derived tokens, and
exercise resource-limit failures. The manifest describes test inputs only and
is not a generated result.
