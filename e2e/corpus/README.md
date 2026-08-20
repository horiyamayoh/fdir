# Independent fidelity corpus

These cases are hand-authored source documents kept outside the generated E2E
fixture builder. The corpus is intentionally small and format-diverse: its
purpose is to catch authority, relationship, status, canonicalization, and
query regressions with source material that the ordinary acceptance generator
does not create.

`tools/independent_corpus.py` packages the OOXML part directories, invokes the
public converter, validates the resulting IR, checks source-derived tokens,
and exercises a resource-limit failure. The manifest is machine-readable and
is part of the release gate.
