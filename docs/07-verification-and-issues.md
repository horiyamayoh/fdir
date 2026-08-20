# 7. 検証計画と Issue 分割

## 7.1 検証の考え方

品質の中心は qualification の事務処理量ではなく、typed model の不変条件と四形式の具体的な変換結果です。受入テストは機械可読要件へ追跡し、成果物がない Issue を完了扱いにしません。

必須テスト領域:

- schema tests、generated type parity、required/unknown field tests
- invariant tests、ID/reference/containment/order validation
- cross-format normalized mapping tests
- DOCX/XLSX/PDF/Markdown extension tests
- geometry、anchor、wrap、clip、transform、z-order tests
- style inheritance、direct/theme/conditional/resolved tests
- formula/stored/cached/displayed/rendered/observed separation tests
- rendering comparison tests（近似状態を含む）
- malformed input、partial conversion、resource limit tests
- deterministic serialization、unknown extension、backward compatibility tests
- performance/resource tests

## 7.2 受入判定

Issue は次をすべて満たした場合だけ完了です。

1. 担当 requirement が machine/requirements.json にあり、test と issue が割り当てられている。
2. 正規文書または schema の変更がある場合、内容と生成物が同期している。
3. 実装と positive/negative/partial fixture がある。
4. preserved と normalized、unsupported と failed を誤って成功へ変換しない。
5. no raw-byte storage、no semantic interpretation、no free property bag の review が通る。
6. python tools/validate_design.py と該当 test が再現可能に pass する。
7. 未対応の責務・loss・ambiguity が明示され、次の owner Issue がある。

## 7.3 実装パイプライン

~~~mermaid
flowchart LR
  req[Requirements] --> schema[Schema + typed model]
  schema --> invariants[Invariants]
  invariants --> adapters[Four adapters]
  adapters --> examples[Concrete fixtures]
  examples --> query[Query / export]
  query --> integration[Cross-format integration]
  integration --> release[Release readiness]
~~~

## 7.4 Issue の分解原則

- 設計凍結、core schema、canonicalization、extension registry、status/report、各形式 adapter、query/index、test/release を分離する。
- 形式 adapter は DOCX/XLSX/PDF/Markdown を混ぜず、共通 core の先行 Issue に依存する。
- Semantic IR、equivalence、lineage、raw-byte archive は Issue plan の範囲外とする。
- 一つの Issue は一つの owner、明確な path、明確な acceptance を持つ。
- 120 件以上の requirements を、内容を削らず 20 前後の実装 Issue に束ねる。Issue 数を減らすために requirements を統合しない。

詳細な requirement-to-test-to-issue の対応は machine/requirements.json、machine/acceptance-tests.json、machine/issue-plan.json にあります。
