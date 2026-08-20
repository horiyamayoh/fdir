# 4. DOCX・XLSX・PDF・Markdown マッピング

## 4.0 実装範囲と qualification の境界

この章の表と例は、common core と形式拡張の mapping contract、想定する
loss/ambiguity、必要な diagnostic を記述します。表に行があることや、例に
`status: preserved` があることは、すべての実装・形式機能が実装済みまたは
qualified であることを意味しません。

The repository currently has bounded adapter paths for all four named formats,
but release qualification is explicitly blocked by
[`machine/audit-recovery-plan.json`](../machine/audit-recovery-plan.json).
Implementation presence, a synthetic example, or a selected real-input check
is not evidence of complete coverage. In particular, this chapter does not
claim relationship completeness, source-faithful reconstruction, or full
ECMA-376/PDF/CommonMark-GFM conformance. Unsupported, unavailable, ambiguous,
and approximated constructs remain part of the expected contract until the
applicable recovery evidence is complete.

## 4.1 共通マッピングの読み方

各 adapter は source construct を直接 any に詰めず、まず common core の typed entity へ写像し、その後で形式固有差異を Extension へ記録します。normalization は記録形式上の差を吸収するだけで、業務意味の同値化ではありません。

## 4.2 DOCX

| source construct | common core mapping | format-specific extension | normalization | known loss / ambiguity | required diagnostic |
| --- | --- | --- | --- | --- | --- |
| document/body/section | Document, Part, Node(document/section) | section property set、section break vocabulary | section identity を stable id 化 | compatibility markup の分岐 | DFIR-DOCX-UNSUPPORTED-MARKUP |
| paragraph/run/text | Node(paragraph/run), Text | XML run property、field code fragment | UTF-8 と text fragment へ分割 | field result と instruction の関係が不完全な場合 | DFIR-DOCX-FIELD-PARTIAL |
| table/row/cell | TableGrid, Node(table/row/cell) | tblGrid、merge/vMerge、table style | row/column index を明示 | 複雑な merge の表示依存 | DFIR-DOCX-MERGE-AMBIGUOUS |
| style/based-on/theme/direct | Style の authored/basedOn/theme/direct/resolved | Word style id、latent style、theme slot | style inheritance の順序を固定 | renderer の computed style unavailable | DFIR-STYLE-RESOLUTION-INCOMPLETE |
| numbering/list | Node(list) と Order | numbering definition、abstract/instance id | list level と source order を保持 | restart 条件が不明 | DFIR-DOCX-NUMBERING-AMBIGUOUS |
| header/footer/footnote/endnote | Part と Annotation | relationship、reference id | target relation を typed 化 | page-specific resolution が未観測 | DFIR-DOCX-REFERENCE-UNRESOLVED |
| comment/revision | Annotation(comment/revision) | author、date、revision mark | displayed view と authored revision を分離 | accept/reject は view selection | DFIR-DOCX-REVISION-VIEW |
| hyperlink/bookmark/control | Annotation(link/bookmark/control) | relationship、bookmark name、control tag | target と displayed text を分ける | external target unavailable | DFIR-DOCX-TARGET-UNAVAILABLE |
| drawing/shape/text box/connector | Node(shape/text-box/connector), Geometry, Layout, Relation | DrawingML/VML properties | coordinate/anchor を common length へ正規化 | wrap/anchor の renderer 差 | DFIR-DOCX-GEOMETRY-APPROX |
| equation/embedded object | Node/Resource + Extension | OMML、OLE、linked object | resource description と node を分離 | object content not inspected | DFIR-DOCX-OBJECT-UNAVAILABLE |

### Word の曲がった矢印付き吹き出し

吹き出しは意味を持つ assertion ではなく、形状・テキスト領域・connector path・anchor・z-order という複数の形式事実です。最小例は examples/callout.json にあります。

~~~json
{
  "nodes": [
    {"nodeId":"shape_callout","kind":"shape","geometryId":"geo_callout","layoutId":"layout_callout","status":"preserved"},
    {"nodeId":"text_callout","kind":"textBox","textIds":["text_note"],"geometryId":"geo_text_region","status":"preserved"},
    {"nodeId":"connector_1","kind":"connector","geometryId":"geo_curve","layoutId":"layout_connector","status":"normalized"},
    {"nodeId":"paragraph_target","kind":"paragraph","status":"preserved"}
  ],
  "geometries": [
    {"geometryId":"geo_callout","spaceId":"page_1","primitives":[{"kind":"roundedRectangle","x":{"value":"120","unit":"pt"},"y":{"value":"80","unit":"pt"},"width":{"value":"180","unit":"pt"},"height":{"value":"70","unit":"pt"},"rotation":{"value":"0","unit":"deg"}}]},
    {"geometryId":"geo_curve","spaceId":"page_1","primitives":[{"kind":"bezier","points":[{"x":{"value":"150","unit":"pt"},"y":{"value":"150","unit":"pt"}},{"x":{"value":"100","unit":"pt"},"y":{"value":"180","unit":"pt"}},{"x":{"value":"90","unit":"pt"},"y":{"value":"260","unit":"pt"}}],"arrowhead":"triangle"}]}
  ],
  "layouts": [
    {"layoutId":"layout_connector","targetId":"connector_1","anchor":{"kind":"floating","surfaceId":"page_1","from":{"kind":"shape","nodeId":"shape_callout"},"to":{"kind":"paragraph","nodeId":"paragraph_target","offset":"text:12"},"wrap":"none","zIndex":3}}
  ],
  "relations":[
    {"relationId":"rel_connector_target","kind":"connectorTarget","fromId":"connector_1","toId":"paragraph_target","endpoint":"to","status":"preserved"}
  ]
}
~~~

この構造は「矢印が何を意味するか」「矢印が何を警告するか」を保持しません。

## 4.3 XLSX

| source construct | common core mapping | format-specific extension | normalization | known loss / ambiguity | required diagnostic |
| --- | --- | --- | --- | --- | --- |
| workbook/worksheet | Document, Part(workbook/sheet), Surface | workbook relationship、sheet state | sheet identity と tab order | external link access unavailable | DFIR-XLSX-SHEET-UNAVAILABLE |
| row/column/cell | TableGrid, Node(row/column/cell) | sparse cell address、dimension | coordinate を row/column index へ | implicit empty cell は作らない | DFIR-XLSX-SPARSE-CELL |
| shared/inline string | Text(source) | shared string index | displayed text への参照を維持 | rich text run information | DFIR-XLSX-STRING-RICH-TEXT |
| raw value/formula/cache/display | FormulaField, Text/typed value | formula attributes、calculation chain | values は別 field | stale cache、locale/date ambiguity | DFIR-XLSX-CALC-CONTEXT |
| number format/date system | Style + typed date/time | format code、1900/1904 system | decimal exact value を維持 | serial date の invalid leap day | DFIR-XLSX-DATE-SYSTEM |
| merge/name/table/validation | Relation、TableGrid、Annotation | defined name、table ref | range reference を typed range へ | overlapping merge/invalid ref | DFIR-XLSX-RANGE-INVALID |
| conditional formatting | Style conditional rule | differential style、formula rule | rule と resolved result を分離 | priority/stopIfTrue context | DFIR-XLSX-CONDITIONAL-UNRESOLVED |
| drawing/chart/pivot | Node/Resource/Layout/Geometry | drawing anchor、chart series、pivot cache | cell anchor を two-cell/one-cell に型化 | renderer layout unavailable | DFIR-XLSX-ANCHOR-APPROX |
| comment/note/hyperlink | Annotation | legacy note、threaded comment | target cell と text を分離 | author/thread metadata loss | DFIR-XLSX-ANNOTATION-PARTIAL |
| external reference/calculation | Extension + Diagnostic | calc mode、external link | context object を保持 | no recalculation in IR | DFIR-XLSX-EXTERNAL-UNAVAILABLE |

一つのセルの最低限の構造は examples/cell-formula.json に示します。

~~~json
{
  "nodeId":"cell_B2",
  "kind":"cell",
  "address":{"sheetId":"sheet_data","row":2,"column":2},
  "formulaFieldId":"formula_B2",
  "styleId":"style_currency",
  "status":"preserved"
}
~~~

~~~json
{
  "formulaId":"formula_B2",
  "kind":"spreadsheetFormula",
  "expression":{"source":"=A1+B1","language":"excel-a1","status":"preserved"},
  "values":{
    "raw":{"type":"number","value":"20","status":"preserved"},
    "cached":{"type":"number","value":"20","status":"preserved"},
    "displayed":{"text":"20.00","status":"normalized"}
  },
  "numberFormat":{"code":"0.00","status":"preserved"},
  "calculationContext":{"dateSystem":"1900","locale":"ja-JP","mode":"automatic","status":"preserved"}
}
~~~

## 4.4 PDF

| source construct | common core mapping | format-specific extension | normalization | known loss / ambiguity | required diagnostic |
| --- | --- | --- | --- | --- | --- |
| page/resource | Part(page), Surface | page tree、resource name | page coordinate space を明示 | malformed page tree | DFIR-PDF-PAGE-RECOVERED |
| text fragment/character code | Text(source), Annotation/Extension | content stream operator、CMap | Unicode mapping を別 field にする | missing CMap | DFIR-PDF-UNICODE-MAPPING |
| glyph/position/text matrix | Node(text), Geometry, Observation | font dictionary、glyph id | matrix を affine transform 化 | font metrics unavailable | DFIR-PDF-GLYPH-GEOMETRY |
| path/fill/stroke | Node(path), Geometry, Style | operator sequence | path を typed segment へ | unsupported operator | DFIR-PDF-PATH-UNSUPPORTED |
| image | Resource/image Node/Geometry | XObject metadata | crop/transform を分離 | image decode unavailable | DFIR-PDF-IMAGE-UNAVAILABLE |
| graphics state/transform/clip | Style, Geometry | q/Q、clip operator | graphics context を scope 化 | recovery changes scope | DFIR-PDF-CLIP-RECOVERED |
| annotation/form/outlines/destination | Annotation/Relation | PDF dictionary | target relation を typed 化 | external action unavailable | DFIR-PDF-ACTION-UNAVAILABLE |
| tagged structure/marked content | Node/Order | StructTree、MCID | reading-order candidate と source structure を分離 | tags absent or conflicting | DFIR-PDF-READING-ORDER |
| renderer/OCR | Observation | engine-specific details | source-declared と observation を並列保持 | disagreement | DFIR-PDF-OBSERVATION-DIVERGENCE |

PDF では source-declared character code、glyph、Unicode mapping、placement、renderer result、OCR result を一つの text にしません。clipping path は examples/pdf-observation.json のように geometry と graphics context へ格納します。

## 4.5 Markdown

| source construct | common core mapping | format-specific extension | normalization | known loss / ambiguity | required diagnostic |
| --- | --- | --- | --- | --- | --- |
| heading/paragraph/list | Node(section/paragraph/list), Text | heading marker、indent、tight/loose | block hierarchy を typed tree 化 | dialect extension | DFIR-MD-DIALECT |
| table | TableGrid | delimiter alignment、escaped pipe | cell text と delimiter を分離 | malformed row width | DFIR-MD-TABLE-WIDTH |
| link/image/reference | Annotation/Resource/Relation | reference definition、destination syntax | target と display text を分離 | unresolved reference | DFIR-MD-REFERENCE |
| code/emphasis | Node + Style/Text | fence、backtick count、delimiter | authored marker と rendered style を別管理 | renderer dialect difference | DFIR-MD-DELIMITER |
| raw HTML/front matter | Extension/Resource/Annotation | HTML fragment、YAML/TOML front matter | allowed typed metadata のみ core 化 | raw HTML semantics not interpreted | DFIR-MD-RAW-HTML |
| escaping/entity/line break | Text(source/display), Order | escape spelling、entity spelling | display Unicode と source spelling を別 | renderer normalization | DFIR-MD-SOURCE-SPELLING |

レンダリング結果が近くても delimiter、escaping、reference style、raw HTML の使い方が異なる場合は、共通 core の paragraph/text/style へ写像しつつ、authoring-form facts を Markdown extension に残します。「同じ表示だから同じ意味」とは判断しません。

## 4.6 四形式共通の境界

| 事実 | common core | format extension | downstream Semantic IR |
| --- | --- | --- | --- |
| 同じ文字列の存在 | Text の source/display representation | shared string index、glyph code、Markdown escape | 必要なら概念候補へ参照 |
| 段落・セル・glyph の構造 | Node、Part、Surface、Order | format-specific selector | 文書の意味を解釈する際の入力 |
| 赤い文字 | Style の resolved foreground | theme/direct formatting の source detail | 「警告」等の意味づけ |
| 矢印と吹き出し | shape、connector、geometry、anchor | DrawingML/PDF operator details | 因果・指示対象の意味づけ |
| formula/display | FormulaField の別々の values | Excel formula language/cache details | 数式の業務意味や計算判断 |
| OCR の差異 | Observation と diagnostic | OCR engine payload | 文字の信頼性判断 |
