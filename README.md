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

この変更は製品実装のリセットです。旧 assertion-first／evidence／accounting／source-byte storage 実装は退役させ、上記文書・スキーマ・要件を新しい開発の権威にします。現時点で四形式の変換器が完成したことは主張しません。実装は新 Issue 群を順に完了した時点で追加します。

設計成果物の整合性は次で確認できます。

    python tools/validate_design.py

GitHub issue key/number mapping is recorded in [machine/github-issue-map.json](machine/github-issue-map.json). The disposition of superseded legacy issues is recorded in [machine/legacy-issue-map.json](machine/legacy-issue-map.json).

要件は 120 件を下回らない粒度へ展開し、各要件に受入テストと担当 Issue を割り当てます。Issue を閉じる条件は、実装・テスト・文書・未対応状態の説明がそろい、未所有要件がないことです。

## ライセンスと安全性

ライセンス、脆弱性報告、コントリビューションの規則はそれぞれ [LICENSE](LICENSE)、[SECURITY.md](SECURITY.md)、[CONTRIBUTING.md](CONTRIBUTING.md) を参照してください。入力文書は不可信データとして扱い、parser、renderer、OCR は必要に応じて隔離された adapter 境界へ置きます。
