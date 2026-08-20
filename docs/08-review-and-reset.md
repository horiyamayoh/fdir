# 8. 厳格レビューとリポジトリ移行判断

## 8.1 別人格 reviewer による自己レビュー

### 意味論の混入

指摘: relation、annotation、status が semantic assertion や confidence に戻る危険がある。
修正: relation kind は connectorTarget、styleBasedOn、sourceReference 等の形式関係だけに限定し、意味の predicate を schema に設けない。status は表現状態で、真偽や確信度ではない。

### 原典保存・フォレンジック化

指摘: SourceMap、Resource、extension が raw bytes の保存口になり得る。
修正: bytes reference は IR 外の optional handle に限定し、SourceMap は locator だけ、Resource は形式上の resource 記述だけにする。canonical identity は IR bytes のみ。

### 任意 JSON の袋

指摘: format extension の payload が any の別名になる。
修正: namespace、type、schemaId、schemaVersion、criticality、compatibility と registry schema を必須化し、core field は列挙型 typed object にする。

### 四形式の偏り

指摘: DOCX/XLSX の office model だけが強く、PDF/Markdown が文字列へ潰れる可能性がある。
修正: PDF の glyph/CMap/matrix/path/clip/OCR、Markdown の delimiter/escaping/reference/raw HTML を独立して明示し、四形式の mapping table と fixture を要求する。

### 書式・layout・geometry の弱さ

指摘: rectangle と text だけでは吹き出し、connector、style inheritance、z-order を表せない。
修正: Style、Layout、Geometry、Relation、Order を独立 entity とし、bezier/path、anchor、transform、clip、draw order を typed field 化する。

### formula/value の混同

指摘: Excel の cell を text に変換すると stored/cached/displayed が失われる。
修正: FormulaField の expression、raw、cached、displayed、numberFormat、calculationContext を必須設計とし、fixture で別 query を検証する。

### PDF source fact と観測の混同

指摘: OCR が source text を上書きする。
修正: source Text/glyph と Observation/OCR token を別 entity にし、差異を relation と Diagnostic にする。

### 複雑すぎるモデル

指摘: 全形式の AST、byte census、巨大 enum を同時に保持すると実装不能になる。
修正: common core を複数形式で使う typed fields に限定し、その他は extension として実装単位を分割する。native AST と byte evidence は製品中核から除外する。

### downstream Semantic IR 境界

指摘: downstream が FDIR の relation/status を意味として誤読する。
修正: layer document と schema namespace を公開し、Semantic IR は read-only facts を参照する一方向 boundary とする。

### 未解決事項

- 各形式でどの optional feature を最初の capability profile に含めるか。
- renderer/OCR engine の具体的選定、ライセンス、隔離方式。
- style resolution の形式差をどこまで common core に昇格するか。

これらは設計を曖昧にするものではなく、Issue plan の bounded decision として残します。未決定を成功扱いにしません。

## 8.2 旧リポジトリの扱い

| 分類 | 対象 | 判断 |
| --- | --- | --- |
| 廃棄 | assertion-first logical model、RecordAssertion、equivalence、lineage、accounting/census、content-addressed evidence、source-byte store | 新製品の目的と反するため参照・移植しない |
| 廃棄 | fdir-semantics、fdir-accounting、fdir-storage、旧 adapter protocol の意味論 lane | 名前変更で再導入しない |
| 廃棄 | 旧 release claim、qualification policy、dependency bureaucracy、旧 issue traceability | 新 issue plan で再構成する |
| 設計とは無関係に再利用 | LICENSE、NOTICE、基本の security hygiene、GitHub の issue/PR 操作 | 内容を新製品境界へ更新する |
| 修正して再利用 | deterministic JSON の一般的な考え方、標準ライブラリ quality runner の骨格、CI の fail-fast 構造 | raw-byte/semantic/evidence 前提を除去して設計 validator へ作り替える |
| 参照禁止 | 旧 requirement ID、旧 examples/fixtures、旧 generated contract、旧 migration、旧 query | 新設計へ語彙と責務を漏らすため互換 layer を作らない |

既存コードを救済することは目的にしません。実装の再利用は、上記の新しい schema と境界を満たすことが独立に確認できる場合だけ許可します。
