# 2. アーキテクチャと責務境界

## 2.1 コンポーネント図

~~~mermaid
flowchart LR
  A[DOCX / XLSX / PDF / Markdown] --> B[Parser / Adapter]
  B --> C[Typed Document Form IR]
  B -. optional .-> X[Format Extension Registry]
  X --> C
  C --> V[Validator]
  C --> I[Rebuildable Query Index]
  C --> R[Renderer / Export]
  C --> Q[Query API]
  C -. facts only .-> S[Downstream Semantic IR]
  O[Renderer / OCR Observation] --> C
  M[Optional Source Map] -. debug locator .-> C
~~~

## 2.2 データフロー

~~~mermaid
flowchart TD
  input[入力文書] --> detect[形式判定と capability negotiation]
  detect --> parse[形式 parser]
  parse --> core[共通 core への typed mapping]
  parse --> ext[namespace 付き extension mapping]
  core --> merge[IR assembler]
  ext --> merge
  observe[renderer / OCR] --> obs[Observation entities]
  obs --> merge
  merge --> report[ConversionReport + Diagnostic]
  report --> canonical[deterministic canonical serialization]
  canonical --> index[非権威 index]
  canonical --> consumer[Query / Export / downstream Semantic IR]
~~~

## 2.3 層の所有権

| 層 | 所有するもの | 所有しないもの | 境界条件 |
| --- | --- | --- | --- |
| Parser / Adapter | 入力形式の構文を読み、source-declared structure、authoring facts、形式上の値・書式・配置を typed IR へ変換 | 意味解釈、業務判断、IR 外の証拠保全 | adapter は DocumentForm と ConversionReport を返す。入力バイトを IR に埋め込まない |
| Document Form IR | 共通 core、format extension、source map、state、diagnostic、観測とのリンク | 意味論、完全復元、原典 archive | IR は自己完結した形式事実の表現。欠落があっても report に可視化 |
| Renderer / Query / Export | IR の表示、検索、別表現への出力、観測 entity の生成 | source fact の上書き、semantic assertion の確定 | renderer output は observation。query index は再構築可 |
| Semantic IR | Document Form IR の typed facts を参照し、概念・意図・業務意味を別モデルで表現 | 形式事実の改ざん、FDIR の状態を成功へ書き換えること | 一方向参照。FDIR は Semantic IR に依存しない |

## 2.4 権威関係

~~~mermaid
flowchart LR
  source[入力形式の source-declared facts] --> adapter[Parser / Adapter]
  adapter --> ir[Document Form IR が形式事実の権威]
  ir --> canonical[IR canonical JSON]
  canonical --> index[Query index: 非権威]
  ir --> semantic[Semantic IR: downstream interpretation]
  observe[Renderer/OCR observations] --> ir
  observe -. source fact を置換しない .-> ir
  meta[Ingestion metadata] -. IR 外 .-> adapter
  bytes[原典バイト store] -. IR の必須要素ではない .-> meta
~~~

IR canonical digest は canonical IR bytes から算出できます。入力ファイルの hash、保存場所、キャッシュキー、取り込み時刻は ingestion metadata の範囲であり、DocumentForm.documentId の identity material ではありません。

## 2.5 Adapter interface

実装言語は固定しません。core contract は schema が権威で、adapter は次の論理 interface を満たします。

~~~text
inspect(input_handle) -> CapabilityReport
convert(input_handle, profile) -> ConversionResult

ConversionResult {
  document_form: DocumentForm,
  report: ConversionReport,
  diagnostics: [Diagnostic]
}
~~~

input_handle は adapter 境界の外側で管理される opaque handle です。adapter が raw_bytes を結果へコピーする contract は作りません。parser が認識できない部分は unsupported、入力が壊れて読めない部分は failed または unavailable として report に記録します。

## 2.6 隔離境界

- parser は不可信入力を読むため、必要なら別 process / sandbox で動かす。
- renderer、OCR、フォント依存の観測は replaceable worker とし、結果は Observation へ格納する。
- worker のバージョン・設定・resource limit は ingestion metadata または report の実行情報で、文書形式事実の entity と混同しない。
- network、filesystem、CPU、memory、出力サイズ、timeout、cancellation を adapter contract の外側から制御できるようにする。

## 2.7 実装の推奨

core schema、canonicalizer、validator、query planner は Rust を第一候補とします。これは型安全性、境界検証、決定的 serialization、resource control に向くためであり、既存 Rust 採用の惰性的継承ではありません。形式 parser や OCR は性能・ライセンス・安全性を確認したうえで別言語でもよく、process adapter の wire contract だけを共有します。
