このリポジトリ（async-compute-demo）をレビューしてください。面談で見せるデモなので、AWS の実環境でそのまま動くか、設計の説明に穴がないかを重視します。修正はせず、指摘を重要度順に、ファイルと行番号つきで挙げてください。

特に確認してほしい点（作成者自身が確信を持てていない箇所）:

1. terraform/batch.tf の retry_strategy。Fargate Spot が中断されたときの statusReason が本当に "Your Spot Task was interrupted" で始まるか。違うなら Spot 中断時にリトライされない。
2. AWS Batch のサービスロールを指定していない。Fargate の計算環境でサービスリンクロールが自動で使われる前提で正しいか。
3. app/backends.py の BatchBackend。配列ジョブ＋dependsOn の集約ジョブの組み方と、describe_jobs の結果から状態を決める処理に漏れがないか（RUNNABLE、STARTING、片方だけ失敗、など）。
4. terraform/iam.tf の権限。API の batch:SubmitJob のリソース指定（ジョブ定義のリビジョン付き ARN）で実際に投入できるか。足りない権限、広すぎる権限はないか。
5. app/compute.py の naive と vectorized が同じ乱数列を使っているという前提（standard_normal を連続で呼ぶのと、まとめて呼ぶのが同じ値になるか）。
6. app/api.py の同期エンドポイント。重い計算を def で受けたときのスレッドプールの挙動と、デモとしての説明（「async def だとイベントループが止まる」）が正確か。
7. Dockerfile と terraform の組み合わせ（linux/amd64、ポート、環境変数）で、ビルドからデプロイまで通るか。Docker のビルドと AWS へのデプロイは未検証。
8. CloudWatch の Embedded Metric Format の出力形式と、monitoring.tf のダッシュボード定義（"..." の省略記法、stat の指定）が正しいか。

検証済みなのは次の 3 つだけです。pytest 4 件の成功、ローカルでの API の動作確認、terraform validate（aws provider 5.80.0）。
