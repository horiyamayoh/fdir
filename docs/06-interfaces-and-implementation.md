# 6. 問い合わせ、実装境界、API

## 6.1 代表的な問い合わせ

問い合わせは entity kind、typed field、relation、status を使います。意味解釈の predicate を追加しません。

| 問い合わせ | 主な条件 |
| --- | --- |
| すべての段落 | Node.kind = paragraph |
| すべてのセルと数式 | Node.kind = cell、FormulaField.kind = spreadsheetFormula |
| 赤字になっている文字列 | Text の対象 Node と Style.resolved.foreground の color |
| 特定ページ上の図形 | Surface.partId と Node.kind in shape/textBox/connector |
| ある要素を指すコネクター | Relation.kind = connectorTarget、toId = target |
| 非表示要素 | Style.visibility または Layout.visibility = hidden |
| 未対応・近似要素 | status in unsupported/approximated/ambiguous |
| 特定形式の拡張を持つ要素 | Extension.namespace/type と targetId |
| PDF glyph と OCR の不一致 | source Text/glyph と Observation.kind = ocrToken の relation |
| Word style 継承元 | Style.basedOn を辿る |
| Excel 表示値と格納値 | FormulaField.values.raw と values.displayed を別々に取得 |

例として、red text query は Text -> Node -> Style(resolved.foreground) の参照を使います。「red means warning」は query に入りません。

## 6.2 Query API の概念

~~~text
list_nodes(document_id, kind?, part_id?, status?) -> Node[]
get_text(node_id, representation: source|normalized|displayed) -> Text
get_styles(node_id, stage: authored|inherited|theme|direct|resolved) -> StyleView
find_relations(kind, from_id?, to_id?) -> Relation[]
find_extensions(namespace?, type?, target_id?) -> Extension[]
find_observation_differences(target_id?, observation_kind?) -> Difference[]
~~~

Query は index が利用できる場合でも canonical IR の entity と一致することを検証します。index は semantic interpretation を保存する場所ではありません。

## 6.3 推奨 module / crate / package boundaries

実装開始時の境界は次のとおりです。名前は実装言語に拘束されず、責務だけを凍結します。

| module | 責務 | 依存してよいもの | 依存してはいけないもの |
| --- | --- | --- | --- |
| form-schema | JSON Schema、registry、generated types | schema generator | parser vendor、Semantic IR |
| form-core | typed entities、IDs、status、invariants | form-schema | raw byte store、business ontology |
| form-canonical | canonical JSON、digest、version | form-core | source archive、semantic comparator |
| adapter-contract | inspect/convert wire protocol、capability | form-core | format implementation |
| adapter-docx | DOCX parser と mapping | adapter-contract、docx extension schema | semantic assertion engine |
| adapter-xlsx | XLSX parser と mapping | adapter-contract、xlsx extension schema | recalculation authority |
| adapter-pdf | PDF parser と mapping | adapter-contract、pdf extension schema | OCR inference as source fact |
| adapter-markdown | Markdown parser と mapping | adapter-contract、markdown extension schema | business meaning |
| observation-worker | renderer/OCR observation | adapter-contract | source fact mutation |
| form-validation | schema/invariant/compatibility checks | form-core、form-canonical | qualification bureaucracy as product model |
| form-index | rebuildable query projection | form-core | canonical authority mutation |
| form-cli | inspect/convert/validate/query/export | public modules | direct vendor-specific bypass |
| test-support | fixtures、golden IR、malformed input harness | public contracts | hidden source-byte oracle |

## 6.4 Parser dependency boundary

高水準 library の clean text、cell、paragraph、AST は convenience output です。adapter は:

- parser dependency/version/config を capability metadata に記録する。
- source-declared facts と library-derived normalization を分ける。
- parser の失敗・unsupported を黙って空配列へしない。
- parser output だけで semantic assertion、equivalence、production claim を生成しない。

## 6.5 Renderer / OCR boundary

Renderer/OCR は source-declared structure と同格の権威ではありません。結果は Observation として対象 page/surface/node に関連づけ、engine、version、settings、resource、method、status、precision を記録します。複数 engine の不一致は一つの真値へ平均せず、複数 observation と Diagnostic にします。

## 6.6 CLI / API

初期 CLI は次の bounded command に限定します。

~~~text
fdir inspect <input>
fdir convert <input> --format <kind> --out <document-form.json>
fdir validate <document-form.json>
fdir query <document-form.json> --kind paragraph
fdir explain <document-form.json> --node <id>
fdir export <document-form.json> --target json|markdown|html
~~~

compare、equivalence、lineage、assert、knowledge graph は FDIR CLI の command にしません。構造差・表示差を比較する補助 command を将来追加する場合も、semantic equivalence とは別 namespace と型で設計します。
