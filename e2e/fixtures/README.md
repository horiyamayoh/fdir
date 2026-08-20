# FDIR real-input fixtures

Run `python tools/generate_e2e_fixtures.py` to create deterministic DOCX,
XLSX, PDF, and Markdown inputs plus malformed and unsupported-feature
variants. These files are consumed by the adapter E2E runner; they are not
pre-authored IR fixtures.

The generated binary files are intentionally kept out of the IR model.  The
E2E evidence sidecar records only input metadata and the adapter execution
result.
