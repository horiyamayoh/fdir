# Packaging and generated review artifacts

FDIR 2.1 is distributed from the repository's normative source form. The canonical authority hierarchy is defined by `README.md`, `baseline.yaml`, the machine registries, and the source specifications. Generated review or delivery artifacts do not replace those sources.

## Non-canonical generated artifacts

The following outputs are **non-canonical** projections and may be omitted from a source checkout without losing normative information:

- PDF and DOCX review editions generated from the Markdown specifications and generated references.
- PNG renderings generated from the Mermaid sources in `diagrams/`.
- Search indexes, SQLite databases, JSON-LD projections, rendered reports, and other rebuildable review conveniences.
- Binary qualification artifacts, including captured executables, archives, corpora, traces, screenshots, and tool-specific evidence bundles.

Generated schemas and references are a special case: they are normative only when byte-identical to regeneration from `machine/logical-model.yaml` using the pinned `tools/generate_contracts.py`. Their authority is therefore derived and mechanically checked, not independent.

## Repository and release packaging

A source package must contain the normative text and machine-readable authority needed to run:

```bash
python3 tools/validate_baseline.py .
```

Review packages may additionally contain generated PDF, DOCX, or PNG files under ignored output directories such as `dist/` or `reports/`. Qualification packages may contain binary qualification evidence under ignored qualification paths. These packages must identify the source revision, generation command, tool versions, and checksums, and must state that the generated files are non-canonical.

Omitting a generated review projection is not loss of canonical information when the authoritative source and deterministic generator remain present. Conversely, a generated artifact must never be used to silently override or repair conflicting normative source.
