# FDIR — Document Form IR

FDIR は、DOCX、XLSX、PDF、Markdown などに記録された構造・表現・配置・表示・作成上の事実を、形式差を吸収した型付き・検証可能・機械可読な **Document Form IR** へ変換するための設計と実装のリポジトリです。

一文で言えば、**FDIR は文書が何を意味するかを推論せず、文書がどのような形式で記録・配置・表示されているかを共通表現にする製品**です。

## 重要な境界

| 層 | 所有するもの | 所有しないもの |
| --- | --- | --- |
| Parser / Adapter | 入力形式を読み、形式上の事実を typed core と形式拡張へ変換する | 業務上の意味、真偽、因果、要求判定 |
| Document Form IR | 構造、文字、表、書式、スタイル、配置、幾何、描画順、数式の各表現、変換状態 | 元バイトの保管、意味論、cross-format semantic equivalence |
| Renderer / Query / Export | IR の表示・検索・出力、観測結果の付加 | source-declared fact の上書き、意味の確定 |
| Semantic IR | 将来の downstream 層。概念・意図・業務ルールを解釈する | FDIR の形式事実を権威として再定義すること |

## 非目標

- 元ファイル全体のバイト列、ZIP 物理配置、PDF の未参照バイトを IR の中核に保存しない。
- 原典の完全復元、exact round trip、フォレンジック証拠保全を製品目標にしない。
- predicate: string、value: any、自由な property bag を共通モデルにしない。
- 赤字が警告、矢印が因果、文章が要求、といった意味を推論しない。
- 異形式文書の業務的同値性、正しさ、矛盾、コード生成を判定しない。

原典 hash、キャッシュキー、入力管理情報が運用上必要な場合は、IR 外側の任意の ingestion metadata として扱います。IR の digest は IR の決定的表現から計算できますが、原典バイト digest を IR identity に含めません。

## 正規文書

- [製品定義と用語](docs/01-product-definition.md)
- [アーキテクチャと層境界](docs/02-architecture.md)
- [型付き論理モデル](docs/03-logical-model.md)
- [四形式のマッピング](docs/04-format-mapping.md)
- [シリアライズ・拡張・状態](docs/05-serialization-and-extensions.md)
- [問い合わせ・実装境界](docs/06-interfaces-and-implementation.md)
- [検証計画と Issue 分割](docs/07-verification-and-issues.md)
- [厳格レビューとリポジトリ移行判断](docs/08-review-and-reset.md)
- [機械可読要件](machine/requirements.json)
- [受入テスト](machine/acceptance-tests.json)
- [Issue 設計](machine/issue-plan.json)
- [JSON Schema](schemas/document-form-ir.schema.json)

## 現在の状態

> **Release status: BLOCKED.** This repository is not release-qualified.

The controlling recovery record is
[`machine/audit-recovery-plan.json`](machine/audit-recovery-plan.json). It
marks release as blocked under umbrella Issue #87 and requires the
commit-bound, live-state qualification program in Issues #88–#105. A closed
Issue, a file’s existence, a field or enum being present, or a command exiting
zero is not sufficient release evidence.

この変更は製品実装のリセットから始まりました。旧 assertion-first／evidence／accounting／source-byte storage 実装は退役させ、上記文書・スキーマ・要件を開発の権威にしています。現在は四形式向けの bounded standard-library adapter paths が実装されていますが、完全な形式対応や release qualification が完了したことは主張しません。

設計成果物の整合性は次で確認できます。

    python tools/validate_design.py

### Implemented adapter surface (not qualified evidence)

The repository contains bounded standard-library adapter paths for DOCX, XLSX,
PDF, and Markdown through `tools/convert_document.py`. These paths are an
implemented reference surface and may exercise selected real-input fixtures;
they are not a claim of complete format coverage, standards conformance,
relationship completeness, source-faithful reconstruction, or release
readiness. Unsupported or unavailable features must remain explicit diagnostics
or residual states, but their presence in the model does not by itself qualify
the adapter.

The historical reset note above describes the start of this workstream. The
current implementation surface and the qualification state are separate:
implementation work may be present while the release remains blocked.

### Local diagnostic commands and release gate

The following commands are reproducible local checks from the repository root:

    python tools/validate_design.py
    python tools/run_acceptance.py --all
    python tools/run_e2e.py --all
    python tools/release_gate.py

`validate_design.py` checks the declared authority graph (134 requirements, 16
acceptance families, and the historical 20-issue implementation plan).
`run_acceptance.py --all` and `run_e2e.py --all` exercise the declared
acceptance matrix and bounded real-input paths. `release_gate.py` combines
those checks, but none of these command results overrides the recovery plan or
constitutes the required commit-bound qualification bundle. Release remains
blocked until all #88–#105 evidence, dependencies, and live Issue state
satisfy that plan.

### Real-input conversion

The public bounded adapter entry point is:

    python tools/convert_document.py inspect <input> [--format <kind>] [--profile <id>]
    python tools/convert_document.py convert <input> --out document-form.json [--format <kind>] [--profile <id>] [--evidence execution.json]
    python tools/convert_document.py validate document-form.json

When requested, the evidence sidecar is ingestion metadata outside the IR. It
records the input path, size, SHA-256, adapter module, and whether the file was
consumed; source bytes are never copied into the IR.

GitHub issue key/number mapping is recorded in [machine/github-issue-map.json](machine/github-issue-map.json). The disposition of superseded legacy issues is recorded in [machine/legacy-issue-map.json](machine/legacy-issue-map.json). Those maps are traceability inputs, not release qualification evidence.

The 120-plus requirement inventory and historical issue plan describe intended
scope. Current closure is governed by the audit recovery plan: implementation,
source-fact coverage, negative evidence, reproducibility, and live Issue state
must all be demonstrated before a release claim is restored. The following
claims are therefore intentionally not made: `production-ready`, `complete`,
`zero silent loss`, `relationship-complete`, `source-faithful`, `universal
query`, `independent qualification`, `full CommonMark/GFM`, `full PDF 1.7`,
and `full ECMA-376`.

要件は 120 件を下回らない粒度へ展開し、各要件に受入テストと担当 Issue を割り当てます。Issue を閉じる条件は、実装・テスト・文書・未対応状態の説明がそろい、未所有要件がないことです。

## ライセンスと安全性

ライセンス、脆弱性報告、コントリビューションの規則はそれぞれ [LICENSE](LICENSE)、[SECURITY.md](SECURITY.md)、[CONTRIBUTING.md](CONTRIBUTING.md) を参照してください。入力文書は不可信データとして扱い、parser、renderer、OCR は必要に応じて隔離された adapter 境界へ置きます。
