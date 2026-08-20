# Contributing to FDIR

FDIR は Document Form IR の設計と実装を行います。変更は、文書に記録された構造・表現・配置・表示・作成形式上の事実に限定してください。

## 変更の前提

- 変更には machine/issue-plan.json の owner Issue を一つ割り当てる。
- machine/requirements.json の要件、machine/acceptance-tests.json の受入 family、対象 path を Issue に記載する。
- Semantic IR、業務意味、原典 byte archive、content-addressed source store、byte census、accounting closure、semantic equivalence を追加しない。
- core schema に自由な property bag、predicate/value、未型付けの any を追加しない。
- source-declared、normalized、rendered、observed、未対応の状態を混同しない。
- SourceMap、ingestion metadata、index を IR identity の権威にしない。

## 変更手順

1. Issue を選び、所有責務と対象外を確認する。
2. 要件・schema・例・受入テストを更新する。
3. python tools/validate_design.py を実行する。
4. 該当 adapter、schema、invariant、partial/unknown extension のテストを実行する。
5. canonical output と stable diagnostic code をレビューする。
6. PR には変更した要件、テスト結果、既知の loss、未対応を記載する。

## Issue 完了

Issue は、所有要件が実装・検証され、部分変換や未知拡張が明示され、未所有の責務が残らない場合だけ完了です。未実行、skip、deferred、単なるドキュメント記載だけでは完了としません。
