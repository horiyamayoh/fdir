# Development

## Design authority

1. schemas/document-form-ir.schema.json
2. machine/requirements.json
3. machine/acceptance-tests.json
4. machine/issue-plan.json
5. docs/01-product-definition.md から docs/08-review-and-reset.md

生成 projection、query index、fixture manifest、CI report はこれらの権威を置き換えません。

## Current phase

現在は旧 FDIR 2.1 の実装を退役させ、Document Form IR の型付き設計・要件・Issue 境界を整えるリセット段階です。四形式の parser、renderer、OCR が完成したという主張はありません。

## Validation

    python tools/validate_design.py

実装 Issue が進んだ後は、schema validation、invariant tests、format fixture、partial conversion、unknown extension、deterministic serialization、resource-limit tests を追加します。

## Implementation choice

設計は言語非依存です。Rust は core model、canonicalization、validation、query runtime の候補ですが、既存 Rust workspace の互換維持は目的ではありません。形式 parser、renderer、OCR は安全性・依存・性能を検証したうえで別 process または別言語でも構いません。
