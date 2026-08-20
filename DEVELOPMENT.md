# Development

## Design authority

1. schemas/document-form-ir.schema.json
2. machine/requirements.json
3. machine/acceptance-tests.json
4. machine/issue-plan.json
5. machine/release-gate.json
6. docs/01-product-definition.md から docs/08-review-and-reset.md

生成 projection、query index、fixture manifest、CI report はこれらの権威を置き換えません。

`machine/audit-recovery-plan.json` is the current release-status guard. It does
not replace the model authority above; it records why release qualification is
blocked, which recovery Issues (#88–#105) must be satisfied, and which claims
must not be made before then.

## Current phase

現在は、旧 FDIR 2.1 の実装を退役させた後の、型付き設計・bounded adapter 実装・監査復旧を並行する段階です。リリース資格はまだありません。

The repository includes bounded standard-library adapter paths for real DOCX,
XLSX, PDF, and Markdown inputs, plus a public conversion command and a
real-input test path. This is implemented surface only. It does not establish
complete format coverage, standards conformance, relationship completeness,
source-faithful reconstruction, or release readiness. Unsupported features
must remain diagnostics and conversion residuals; optional renderer/OCR workers
remain observations and do not replace source-declared facts.

The historical reset sentence above describes the starting point of this
workstream. The current implementation surface is described in
`docs/06-interfaces-and-implementation.md`; its qualification status is
controlled separately by the audit recovery plan.

### Release status

Release is explicitly **blocked**. Do not convert adapter presence, a green
design check, a closed Issue, or a command exit into a release claim. The
recovery plan requires a commit-bound qualification bundle and current live
Issue state for #88–#105, with dependencies satisfied. Until that program is
complete, the repository must not claim production readiness, completeness,
zero silent loss, relationship completeness, source faithfulness, universal
query coverage, independent qualification, or full standards coverage.

## Validation

The following are local diagnostic and regression checks, not standalone
release qualification:

    python tools/validate_design.py
    python tools/run_acceptance.py --all
    python tools/run_e2e.py --all
    python tools/release_gate.py

The declared design inventory contains 134 cases across 16 acceptance
families. `run_acceptance.py --all` exercises that inventory when the local
environment is available. `run_e2e.py --all` exercises bounded real-input
cases across the four adapter paths, including malformed and resource-limit
outcomes. Use
`--family AT-<family> --case <number>` to reproduce one acceptance case and
`--json` to archive a stable machine-readable result. `release_gate.py` is the
fail-closed integration command used by CI and includes the real-input path and
boundary review, but it cannot override the explicit release block in the
recovery plan.

実装 Issue が進んだ後は、schema validation、invariant tests、format fixture、partial conversion、unknown extension、deterministic serialization、resource-limit tests を追加します。

## Implementation choice

設計は言語非依存です。Rust は core model、canonicalization、validation、query runtime の候補ですが、既存 Rust workspace の互換維持は目的ではありません。形式 parser、renderer、OCR は安全性・依存・性能を検証したうえで別 process または別言語でも構いません。
