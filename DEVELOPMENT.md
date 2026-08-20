# Development

## Design authority

1. schemas/document-form-ir.schema.json
2. machine/requirements.json
3. machine/acceptance-tests.json
4. machine/issue-plan.json
5. machine/release-gate.json
6. docs/01-product-definition.md から docs/08-review-and-reset.md

生成 projection、query index、fixture manifest、CI report はこれらの権威を置き換えません。

## Current phase

現在は旧 FDIR 2.1 の実装を退役させ、Document Form IR の型付き設計・要件・Issue 境界を整えるリセット段階です。四形式の parser、renderer、OCR が完成したという主張はありません。

The implementation now includes bounded stdlib adapters for real DOCX, XLSX,
PDF, and Markdown inputs, plus a public conversion command and real-input E2E
gate. Unsupported features are represented by diagnostics and conversion
status. Optional renderer/OCR workers remain observations and are marked
unavailable when not configured; they do not replace source-declared facts.

The historical reset sentence above describes the starting point of this
workstream. The current implementation status is the bounded four-format
adapter and E2E path described here and in `docs/06-interfaces-and-implementation.md`.

## Validation

The complete local gate is:

    python tools/validate_design.py
    python tools/run_acceptance.py --all
    python tools/run_e2e.py --all
    python tools/release_gate.py

`run_acceptance.py --all` must report 134 passing cases across 16 acceptance
families. `run_e2e.py --all` must report real-input cases across all four
formats, including malformed and resource-limit outcomes. Use
`--family AT-<family> --case <number>` to reproduce one acceptance case and
`--json` to archive a stable machine-readable result. `release_gate.py` is the
fail-closed integration command used by CI and includes the real-input E2E and
boundary review.

実装 Issue が進んだ後は、schema validation、invariant tests、format fixture、partial conversion、unknown extension、deterministic serialization、resource-limit tests を追加します。

## Implementation choice

設計は言語非依存です。Rust は core model、canonicalization、validation、query runtime の候補ですが、既存 Rust workspace の互換維持は目的ではありません。形式 parser、renderer、OCR は安全性・依存・性能を検証したうえで別 process または別言語でも構いません。
