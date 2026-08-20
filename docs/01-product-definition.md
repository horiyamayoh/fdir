# 1. 製品定義・責務・用語

## 1.1 製品定義

FDIR は、文書ファイルに「記録されている形式上の事実」を抽出し、形式に共通する部分を typed common core へ、形式固有で共通化できない部分を versioned namespace extension へ格納する Document Form IR です。

ここでいう形式は、ZIP のレコード配置や入力バイト列ではありません。文書として記録された構造、文字列、書式、スタイル、配置、座標、図形、表示、作成上の構文です。

## 1.2 解決する問題

- DOCX の段落・run、XLSX のセル、PDF の glyph、Markdown の block を同じ query surface から扱う。
- 共通化できる構造・文字・表・書式・layout・geometry・order を形式間で参照可能にする。
- 共通化できない fields、raw source notation、glyph mapping、package metadata などを捨てずに形式拡張へ隔離する。
- formula、stored value、cached result、displayed text、rendered observation を混同しない。
- 未対応、近似、曖昧、失敗を文書全体の失敗へ潰さず、対象 feature／node／field に付与する。
- 下流の Semantic IR が、形式事実の出所と状態を確認したうえで参照できる境界を提供する。

## 1.3 解決しない問題

FDIR は次を判定・推論・保証しません。

- 文書の概念、意図、業務ルール、真偽、矛盾。
- 赤字が警告、矢印が因果、表が要求仕様、という意味。
- 異なる形式の文書が業務的に同じ内容かどうか。
- 元ファイルのバイト同一復元、原典の完全アーカイブ、デジタルフォレンジック。
- 未対応機能を「対応済み」とすること、または不足した観測を推測で埋めること。

## 1.4 中心原則

1. 共通モデルの field は型を持ち、自由な string/any bag で代替しない。
2. Structure、presentation、layout、geometry、order は別の事実として表す。
3. authored、normalized、computed、displayed、rendered、observed を混ぜない。
4. common core は複数形式で意味を持つ概念だけに限定する。
5. format extension は namespace、schema id、schema version、typed payload、criticality、互換性規則を持つ。
6. IR の identity は canonical IR の内容に基づき、原典バイト digest に依存しない。
7. 形式全体、feature、node、field の状態を必要な粒度で明示する。
8. source map は任意の調査用参照であり、IR の canonical identity や成立条件ではない。
9. index は再構築可能な非権威 projection とする。
10. universal、full fidelity は宣伝語ではなく、明示した capability と status の範囲でのみ使用する。

## 1.5 用語

| 用語 | 定義 |
| --- | --- |
| document form | 文書に記録された構造・表現・配置・表示・作成形式の総体。意味ではない。 |
| structure | document、section、paragraph、table、cell、list 等の containment と typed node。 |
| presentation | 文字・数値・画像・図形の見た目を決める style、theme、number format、visibility 等。 |
| layout | page/sheet/paragraph 等の surface 上の配置関係、anchor、wrap、order。 |
| geometry | 座標系、単位、変換、point、path、rectangle、glyph box、clip 等の幾何事実。 |
| style | authored style、継承、theme、direct override、conditional rule、resolved result の各段階。 |
| authored form | 作成者が選んだ構文・delimiter・style 指定・formula・field instruction 等。 |
| normalized form | 形式間で安全に共有できるよう順序・単位・表現を正規化した結果。意味の同値化ではない。 |
| observation | renderer、OCR、測定器などが観測した形式上の結果。source-declared fact を置換しない。 |
| source map | 任意の UI/debug 用 locator。入力バイト保全や IR identity ではない。 |
| extension | namespace で管理された typed format/vendor payload。共通 core の代用品ではない。 |
| normalization | 形式差を明示的な規則で吸収すること。失われる差異は status/diagnostic に残す。 |
| approximation | 元の形式事実を安全に近似した表現。preserved と称しない。 |
| fidelity | どの形式事実がどの状態で表現されたかの状態集合。exact round trip の単一尺度ではない。 |
| semantic interpretation | 概念、意図、業務意味、真偽などの解釈。Semantic IR の責務。 |

## 1.6 候補アーキテクチャの比較

| 案 | 形式差吸収 | 固有情報 | 型安全性 | 実装難度 | 採否 |
| --- | --- | --- | --- | --- | --- |
| 単一の正規化 Document Graph | 高いが早期の平坦化を強制 | 失いやすい | enum 肥大化しやすい | 中 | 却下 |
| Native Form Graph + Normalized Form Graph の二層 | 高い | 保持しやすい | 二重 identity と同期が複雑 | 高 | 不採用。必要な native facts は extension で足りる範囲から始める |
| 構造・layout・style の完全分離グラフ | 高い | 保持しやすい | edge 増加で query が難しい | 高 | 一部採用。core の containment と独立 relation に限定 |
| 型付き node/edge + resource の統合 graph | 高い | extension と併用可能 | 明示 field なら高い | 中 | 採用 |
| 形式 AST を保持し、その上に projection | 非常に高い | 高い | AST 依存・意味境界が曖昧 | 非常に高い | 却下。必要な authoring facts のみ typed extension として保持 |

## 1.7 採用案

採用するのは、型付き core node、型付き relation/order、独立した style/layout/geometry、外部 resource 参照、versioned extension を一つの DocumentForm に束ねる構造です。

- Structure は typed Node と containment で表す。
- Style、Layout、Geometry、Order は専用 entity と relation で表す。
- source-level facts は common core に入るものだけ typed core field、残りは形式拡張へ置く。
- extension payload は schema registry で発見可能にし、unknown critical extension は読めても conversion を成功扱いにしない。
- native input AST を必須の権威にせず、adapter の内部実装詳細とする。

これにより、形式差の吸収、形式固有情報の保持、型安全性、実装可能性、downstream 境界を同時に保てます。
