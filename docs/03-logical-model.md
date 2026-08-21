# 3. 型付き論理モデル

## 3.1 トップレベル

DocumentForm は共通 core の配列と、必須性が異なる optional entity を束ねます。主要な field は schema に明示し、未定義 key を core entity に自由追加しません。

~~~json
{
  "schema": {"name": "fdir/document-form", "version": "1.0.0"},
  "documentId": "doc_01J...",
  "sourceFormat": {"namespace": "format", "name": "docx", "version": "ECMA-376"},
  "rootNodeId": "node_document",
  "nodes": [],
  "texts": [],
  "styles": [],
  "layouts": [],
  "geometries": [],
  "relations": [],
  "orders": [],
  "extensions": [],
  "sourceMaps": [],
  "conversion": {"status": "partial", "features": [], "diagnostics": []}
}
~~~

documentId は conversion ごとの安定した IR identity です。同じ入力ファイルの保存 byte、元ファイル hash、path、取り込み時刻は identity に含めません。identity の詳細はシリアライズ文書を参照してください。

## 3.2 エンティティ一覧

| Entity | 主責務 | 必須の代表 field | identity / containment |
| --- | --- | --- | --- |
| Document | 文書全体、source format、root | documentId, sourceFormat, rootNodeId | Document が root。byte digest は持たない |
| Part | section、page、sheet、header 等の区画 | partId, kind, parentPartId?, name? | Document に包含。format-specific part name は extension 可 |
| Surface | page、sheet、canvas 等の座標面 | surfaceId, partId, coordinateSpaceId | Part に包含。geometry の基準面 |
| Node | document/paragraph/run/table/cell/shape 等の typed structure | nodeId, kind, parentId?, status | parent containment。kind ごとに追加 typed entity を参照 |
| Text | 文字列・断片・source/display distinction | textId, content, representation | Node から参照。意味は持たない |
| TableGrid | table、row、column、cell の関係 | tableId, rowIds, columnIds, cellIds | Node と cell coordinate を参照 |
| Style | authored、継承、theme、direct、resolved style | styleId, role, properties | basedOn、appliesTo。property は列挙型 field |
| Layout | anchor、wrap、placement、visibility | layoutId, targetId, anchor, placement | Node/Surface を参照 |
| Geometry | coordinate、transform、shape/path、clip | geometryId, spaceId, primitives | Layout/Node から参照 |
| Resource | 画像、font、chart、embedded/linked object の IR 内表現 | resourceId, kind, availability | 外部 bytes の保管場所ではなく、形式上の resource 記述 |
| FormulaField | formula、field instruction、stored/displayed values | formulaId, kind, expression?, values | Cell/Node から参照。semantic result は持たない |
| Annotation | comment、footnote、revision、link、bookmark、OCR token | annotationId, kind, targetIds, status | target 参照。意味解釈はしない |
| Relation | connector、reference、anchor、style inheritance 等 | relationId, kind, fromId, toId | typed relation。自由 predicate ではない |
| Order | structure/source/read/draw/grid/tab/revision order | orderId, kind, ownerId, items | context と主体を明記 |
| Extension | 形式固有・vendor 固有の typed payload | namespace, type, schemaVersion, schemaId, payload, criticality | entity へ namespaced attachment |
| SourceMap | 任意の source locator | sourceMapId, targetId, locator | canonical identity 外。欠落しても IR 有効 |
| ConversionReport | conversion 全体と feature の状態 | status, features, diagnostics | Document に 1 つ |
| Diagnostic | 未対応、近似、曖昧、失敗の理由 | code, severity, targetId?, message, action | target は任意。秘密情報を含めない |

## 3.3 Typed primitives

| 型 | 表現規則 |
| --- | --- |
| integer | JSON number の整数。範囲を schema で指定し、識別子には使わない |
| decimal | 金額、測定値、number format の exact value は canonical decimal string。指数表記・不要な零を正規化 |
| boolean | 真偽のみ。未定義を false と解釈しない |
| string | UTF-8 Unicode scalar sequence。source text と display text は field を分ける |
| bytes reference | IR 外の optional resource/ingestion handle。raw bytes の inline field は不可 |
| color | RGBA または theme reference を discriminated object で表現。単なる文字列色名は禁止 |
| length | value: decimal-string, unit: pt/px/emu/twip/mm/in。変換規則と精度を report に記録 |
| angle | decimal string と単位。度を canonical unit とし、回転の向きを明記 |
| transform | 2D affine [a,b,c,d,e,f] の decimal-string tuple。適用順を明記 |
| coordinates | spaceId と point/line/path の typed object。座標だけの配列は不可 |
| date/time | numeric serial と ISO representation、date system、locale、timezone、precision を別 field |
| formula | source expression、language、reference style、calculation context を持つ object |
| registry reference | namespace、name、version、known/unknown を明記 |
| typed extension payload | registry の schema id/version に従う object。unknown critical の扱いを宣言 |

丸めは conversion profile の規則に従い、decimal を binary floating point へ暗黙変換しません。geometry の renderer measurement だけが floating point を使う場合は precision と observation method を記録します。

## 3.4 Structure と relation

Containment は parentId と children の typed relation としてのみ扱い、次の order を一つの precedes に潰しません。

~~~text
structure order   document tree の子順
source order      source construct の順
reading order     renderer / authoring context で候補となる順
draw order        paint order
z-order           surface 内の重なり順
grid order        row/column/cell の順
tab order         form navigation 順
revision order    tracked revision sequence
~~~

各 Order は kind、ownerId、items、context、status を持ち、異なる主体・座標系・視点を区別します。

## 3.5 書式とスタイル

Style の properties は自由 map ではありません。schema で列挙した typed subobjects を持ちます。

~~~json
{
  "styleId": "style_para_body",
  "role": "paragraph",
  "origin": "authored",
  "basedOn": "style_normal",
  "theme": {"themeId": "theme_default", "slot": "bodyText"},
  "authored": {"paragraphAlignment": "left"},
  "declaration": {"paragraphAlignment": "left"},
  "resolvedFrom": ["style_normal"],
  "resolved": {"paragraphAlignment": "left"},
  "status": "normalized"
}
~~~

最低限、font family/size/weight/italic/underline/strike、foreground/background、alignment、indentation、spacing、line height、border/fill/stroke/transparency、text direction/writing mode、number format、theme、inheritance、direct、computed、conditional、visibility を別々に保持します。

## 3.6 Status と diagnostic の粒度

entity/feature に許可する status は preserved、normalized、approximated、ambiguous、unsupported、omitted-by-policy、unavailable、failed です。omitted-by-policy は policy により意図的に除外した場合、unavailable は入力または観測の条件で取得できなかった場合に限ります。

- ConversionReport.status: 文書全体の最悪状態を要約する。子状態を隠さない。
- ConversionReport.status の値は complete、complete-with-warnings、partial、failed です。entity/feature status と混同しません。
- FeatureStatus: styles、geometry、formulas 等の feature 単位。
- Node.status: 個々の構造要素の表現状態。
- field status: formula/displayed/OCR 等、値の出所が異なる field に必要な場合だけ付ける。

status は意味の正しさや confidence ではありません。confidence を意味論的に扱う field は設けず、観測 method と候補関係を Observation/Diagnostic で分離します。
