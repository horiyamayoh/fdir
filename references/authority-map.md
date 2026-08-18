# Authority map

FDIR 2.1 separates normative source authority from generated review projections and product implementations.

## Normative hierarchy

1. `machine/logical-model.yaml` together with the pinned `tools/generate_contracts.py` defines the core logical model.
2. `schemas/` and `spec/generated/` are normative only when byte-identical to deterministic regeneration from that authority.
3. `machine/requirements.yaml`, `machine/acceptance-tests.yaml`, profiles, capabilities, ADRs, and migration notes are additional normative registries.
4. `spec/` explains the model, evidence rules, accounting closure, equivalence, identity, and claim boundaries.
5. `examples/`, `fixtures/`, `matrices/`, and `queries/` exercise or project the normative source; they do not silently override it.

## Non-canonical outputs

Rendered PDF, DOCX, PNG, SVG, reports, indexes, databases, and binary qualification bundles are non-canonical generated artifacts. A conflict is resolved in favor of the authoritative repository source and deterministic generators.

This baseline makes no product implementation or qualification claim. Production capabilities require separate implementation and qualification evidence.
