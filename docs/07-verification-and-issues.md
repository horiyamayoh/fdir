# 7. 検証計画と Issue 分割

> **Release status: BLOCKED.** The current state is controlled by
> [`machine/audit-recovery-plan.json`](../machine/audit-recovery-plan.json),
> which requires the commit-bound recovery program in Issues #88–#105.

The verification material below distinguishes implementation checks from
qualification evidence. A present adapter, a passing design check, a closed
Issue, or a command exit alone cannot restore release status.

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

The historical implementation Issue criteria above do not override the current
recovery policy. Release qualification additionally requires the live state of
every recovery Issue #88–#105, the dependency DAG recorded in the recovery
plan, and a resolvable commit-bound qualification bundle with reproducible
reports, source accounting, and negative cases. Closed state, file existence,
field or enum presence, and command-exit-only results are not standalone
evidence.

## 7.5 Executable acceptance and release gate

The machine-readable plan is exercised by the following standard-library-only
commands. They are local diagnostic/regression checks, not standalone release
qualification:

```text
python tools/validate_design.py
python tools/run_acceptance.py --all
python tools/run_e2e.py --all
python tools/release_gate.py
```

`validate_design.py` is the authority check for the historical design graph. It
checks the declared requirement-to-test-to-family-to-owner mapping, the
historical 20-issue implementation plan, the GitHub issue map, schema boundary,
examples, adapter paths, and required documents. `run_acceptance.py --all`
expands the declared 16 families and 134 cases when the local environment is
available. `run_e2e.py --all` invokes the public converter in child processes
for bounded real DOCX/XLSX/PDF/Markdown cases, then checks generated IR,
execution metadata, malformed inputs, and configured resource limits. These
are diagnostic/regression checks; they do not independently qualify all
constructs or the release. A single acceptance case can be reproduced with
`--family AT-<family> --case <number>` and machine-readable output with
`--json`.

`release_gate.py` orchestrates the declared checks and performs the final
integration checks. It is fail-closed: a missing adapter, real-input fixture, execution
evidence, malformed JSON, unexplained partial outcome, critical unknown
extension, unbounded property bag, semantic predicate, source-byte store,
forensic accounting, or lineage certificate is a release blocker. Renderer and
OCR results remain observations and never replace source-declared facts.

The acceptance runner checks the design contract; the public converter and
real-input path exercise the four bounded adapter surfaces. Unsupported or
unavailable capabilities are represented as diagnostics and are not promoted
to success. Issue #68 is a historical E2E tracker; its state does not override
the current #87 recovery block. Release qualification is not restored until
all #88–#105 evidence and live Issue state satisfy the recovery
plan.

### Acceptance vocabulary

The following terms are intentionally written out here so the acceptance
matrix can verify the normative vocabulary without relying on a translated
rendering of this document: FDIR records form facts rather than semantic
meaning; the model is extensible; every normative term has an observable
definition; the query index is non-authoritative; ingestion metadata is outside
the IR; downstream Semantic IR cannot redefine FDIR authority; merged regions,
error values, stale caches, tables, charts, stored values, annotations, xref
history, headings, links, code blocks, and footnotes remain explicit form facts.
An index deletion does not change IR identity, and Markdown raw HTML is retained
without semantic interpretation.

Acceptance token line: semantic meaning; observable definition; merges; pivots;
destinations; unreferenced bytes; lists; images; inline code; line-break.
Additional acceptance vocabulary: glossary; themes; cell anchors; forensic
archive; reference definitions.
Final acceptance tokens: external references; forensic archive; resources.
