# 5. シリアライズ、拡張、identity、状態

## 5.1 Canonical representation

IR の canonical JSON は、次の規則で決定的にシリアライズします。

1. UTF-8、BOM なし、LF 改行。
2. object key は schema-defined order、未定義 key は reject。
3. 配列は entity の identity または明示した order により deterministic sort。
4. decimal は符号、先頭零、末尾零、指数表記を canonicalize。
5. length/angle/transform は単位と精度を保持し、暗黙の float 化をしない。
6. optional field の absent と null を混同しない。null を意味として定義しない限り出力しない。
7. canonical bytes から IR digest を計算する。digest は原典 file bytes から計算しない。

canonical order は readability のための配列順であり、document の source/read/draw order を置き換えません。

## 5.2 Schema version と ID stability

- schema version は major.minor。major は意味を変える破壊的変更、minor は additive field/entity/extension registry の追加。
- Entity ID は canonical serialization の配列位置ではなく、adapter が作る deterministic scoped identifier とする。
- 同じ conversion profile で同じ source construct を再度変換できる場合、ID は source map の path だけに依存せず、親・kind・local key から安定化する。
- source map が欠落した場合も、core entity の ID と canonical identity は有効でなければならない。
- 原典 byte digest、file path、mtime、temporary filename を ID material に含めない。

## 5.3 Backward / forward compatibility

| 変更 | reader | writer | 扱い |
| --- | --- | --- | --- |
| additive optional core field | 旧 reader は無視可 | 新 writer が出力可 | minor version |
| additive node kind | 未知 node が non-critical なら opaque extension 扱い | capability 宣言必須 | minor + registry |
| required field の意味変更 | 旧 reader と互換なし | 旧 version を migration | major |
| enum value 追加 | unknown enum の criticality を確認 | capability で宣言 | minor。critical unknown は partial |
| extension schema version | schema registry の compatibility を検査 | exact schema id/version を出す | policy に従う |
| field rename / unit change | migration が必要 | old version を直接上書きしない | major または explicit migration |

未知 extension は namespace と schemaId を見て解釈できる場合だけ読みます。critical extension を読めない場合、core 部分を返しても conversion status は unsupported または partial であり、成功へ丸めません。non-critical は payload を保留できるが、解釈済みとは称しません。

## 5.4 Extension contract

Extension は最低限次を持ちます。

~~~json
{
  "extensionId": "ext_001",
  "targetId": "node_shape",
  "namespace": "urn:fdir:format:docx",
  "type": "drawingml.shape-properties",
  "schemaId": "urn:fdir:schema/docx/drawingml-shape",
  "schemaVersion": "1.0",
  "criticality": "non-critical",
  "compatibility": {"unknownVersion": "preserve-and-mark"},
  "payload": {
    "presetGeometry": "wedgeRoundRect",
    "adjustments": [{"name":"adj1","value":"0.25"}]
  }
}
~~~

payload は registry に登録された schema の typed object です。extension の existence、namespace、schema id、version、criticality は core validator が検証し、payload field の詳細は extension schema validator が検証します。形式固有概念を巨大な core enum に昇格させないでください。複数形式で再利用可能になったときだけ、migration と compatibility review を経て core promotion します。

## 5.5 Source map と ingestion metadata

SourceMap の locator 例は次のとおりです。

- DOCX: part、XML path、element identity
- XLSX: worksheet、cell、range
- PDF: page、object、operator index
- Markdown: line、column、token span

SourceMap は UI jump、debug、issue investigation 用の任意 metadata です。元ファイル byte、byte range の完全保存、canonical identity、conversion 成立条件ではありません。入力 hash、cache key、source path、ingestedAt、worker build はさらに外側の ingestion metadata へ置きます。

## 5.6 状態と診断

### Entity status

preserved は source fact を共通表現へ直接保持、normalized は明示規則で単位・順序・表現を整えた状態、approximated は安全な近似、ambiguous は複数解釈が残る状態、unsupported は機能を理解できない状態、omitted は policy 上除外、unavailable は入力・権限・resource の制約、failed は変換処理の失敗です。

### Diagnostic

Diagnostic は code、severity、target、message、action、sourceMapId?、relatedIds? を持ちます。confidence、accepted、rejected、semantic equivalence、lineage を Diagnostic の名前だけ変えて再導入しません。

一つの未対応 shape があっても document 全体を失敗にせず、shape node に unsupported、feature に partial、document に partial と report します。入力が読めず root を生成できない場合だけ document status は failed です。

## 5.7 Index

Query index は nodes、texts、styles、surfaces、relations の derived table です。canonical IR から全消去・再構築でき、index の row id や順序は IR identity に影響しません。index の欠落や破損は query failure であり IR failure ではありません。

## 5.8 Migration

version migration は:

1. old schema を検査し、未知 critical extension と欠落必須 field を列挙する。
2. typed migration rule で entity と relation を変換する。
3. lost/approximated/ambiguous field を Diagnostic に出す。
4. canonical JSON を再計算し、old ID と new ID の migration map を外部 receipt として出す。
5. 意味論の推論や raw source-byte archive による埋め合わせをしない。
