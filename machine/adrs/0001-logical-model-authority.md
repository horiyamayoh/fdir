
# ADR 0001: Logical-model authority

Status: accepted for FDIR 2.1.0

`machine/logical-model.yaml` and the pinned `tools/generate_contracts.py` jointly define the core logical contract. Generated JSON Schema, CDDL, SQLite DDL, JSON-LD context, and human reference are normative only when byte-identical to regeneration. Hand edits to generated files are invalid even when the edited file remains syntactically valid.

The model file uses the JSON subset of YAML so the baseline validator requires only the Python standard library. This serialization choice does not reduce its YAML authority.
