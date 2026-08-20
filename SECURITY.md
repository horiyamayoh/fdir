# Security policy

入力文書は不可信データとして扱います。parser、renderer、OCR、フォント処理、圧縮展開を使う実装では、入力サイズ、展開サイズ、CPU、メモリ、filesystem、network、credential、timeout、cancellation、出力サイズを制限してください。

## 製品境界

- FDIR は原典 byte archive や forensic evidence store を提供しません。
- 入力 hash や ingestion metadata は IR 外で管理します。
- renderer/OCR の観測結果は source-declared fact を上書きしません。
- unknown critical extension、malformed input、resource exhaustion は明示的な非成功状態にします。

脆弱性の詳細、機密文書、再現用の顧客データを公開 Issue に投稿しないでください。Repository owner の非公開連絡経路を利用してください。
