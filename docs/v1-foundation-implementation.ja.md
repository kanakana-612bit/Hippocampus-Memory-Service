# Hippocampus Ver1.0 新基盤の実装記録

作成日：2026-09-24

対象計画：`v1-context-database-extension-plan.ja.md` 改訂2

## 現在の到達点

旧経路から独立した`v1`を追加した。新しい架空データ用workspaceで、Raw登録、初期／日次共通解析、staging、根拠検証、投入・公開、履歴・文脈想起、最終入力packetまでを接続した。

**これはVer1.0の開発途中の基盤であり、P6全システム受入完了ではない。** 以下の「確認済み」は架空データで確認した構造・制御の事実を指す。実モデルの意味理解・要約品質を確認したという意味ではない。

既存／過去のHippocampus DBを開かず、実ログ本文を取得せず、内容移行・backfill・旧API互換層を作成していない。旧サービスの設定変更、停止、再起動、切替も行っていない。確認に使った一時サーバーは新しい架空workspaceだけに接続し、確認後に停止した。

## 追加した実装

| 領域 | 実装内容 |
|---|---|
| 独立基盤 | 新DB・新署名鍵・新epoch。既存ディレクトリへのinitを拒否し、未初期化workspaceではSQLiteを開く前に拒否 |
| Raw | UTF-8原byte列、原メッセージ、snapshot digest、source＋sequenceの由来。appendは既存prefix一致が条件 |
| 正規化 | `agent → assistant`、`text → content`、1始まりsequence、安定したevent ID、完全turn・tool呼出し対応 |
| 時刻 | exact／estimated_uniform／approximate_range／unknown。日時不明を補完しない。原精度・timezone出所・不明誤差を区別 |
| 共通解析 | 初期／日次／再解析の同一モジュール、model設定固定、共通prompt／schema、明示的provider、無断fallbackなし |
| staging | モデルの書込みを候補へ限定。原文span・digest・role・版・範囲・context参照を照合。通常検索から隔離 |
| 三つの重み | retrieval_weight／generalization_weight／salienceを分離。未評価候補からの一般化強化を拒否。記憶confidenceはnullで保持 |
| 投入 | 原文一致確認、派生物、時刻版、context、所属map、台帳、監査、receiptを同一transactionで投入 |
| 初期公開 | 全batch完了前の記憶・contextを通常参照へ出さない。公開済み連続区間だけcheckpointを進める |
| 日次差分 | 固定したsnapshotの`(L,U]`を使用。古い時刻で後から届いた新sequenceも対象。時刻訂正でcheckpointは動かさない |
| map | 所属0.8／0.3を独立保存。owner付きFK、target kind／版照合。世代切替と手動・対象外所属の保持 |
| 訂正 | 原文不変のtime revision。古い解析入力・履歴cursor・想起packetを失効。影響する派生物は再確認待ちとして保持 |
| 認可 | owner、audience、私的利用面、送信先を照合。restrictedはモデル送信前に拒否。派生物・contextも現行の原文制約を再照合 |
| 想起 | 語句一致／活性context／edgeの候補和集合。contextだけでも選択可能。状態modifierと恒久所属を分離 |
| 倫理 | recordの五軸を独立保持し、寄与を観測。アクセス拒否はscoreと独立 |
| Raw継続枠 | 既定12,500の変更可能枠。現在はUTF-8 byte数を保守的token上界として表示。完全turn単位で採用 |
| 観測 | 検索選択と最終packetを別操作にし、run／attempt／path_revision・内容digestを記録。provider消費は未観測と表示 |
| 履歴・UI | 日本語部分検索、日時不明への到達、署名cursor、全ページ抽出、直近2turn、画面だけの非表示、時刻訂正、guest表示消去 |
| 運用 | 署名backup、信頼済み公開鍵による検証、別workspaceへのrestore、単一ownerの架空workspace全体の明示消去 |

新APIは`/api/v1`で、既存APIへのroutingはない。通常の起動設定も新たに明示指定する。Web画面は接続tokenを永続保存せず、API応答を`no-store`とする。

## 確認済みの試験

新実装を対象とする自動試験 **39件成功**。Python構文確認と画面JavaScriptの構文確認も成功した。

主な確認内容：

- 不正JSON、二重key、空配列、未知role、DST曖昧時刻の拒否。
- 4発話・30分の0／600／1200／1800秒、N=1、総時間不明、期間のみ、ミリ秒精度。
- 同じ本文の別発話を異なるIDとして保持。
- 原文span・role・digest不一致の投入拒否とstaging隔離。
- 高salienceから一般化weightや記憶confidenceを強める提案の拒否。
- モデル設定の実行中固定と、provider失敗時の無断fallback防止。
- 古い時刻での遅延到着、区間の穴、context_onlyの書込み防止。
- 投入途中の故障による全体rollback、commit再送の重複防止。
- DB commit後・anchor更新前の故障からの回復と冪等再送。
- 複数batchの初期runが最後まで非公開であること。
- 手動所属がモデルによる再分類で上書きされないこと。
- owner違い・target kind違い・範囲外weightのDB拒否。
- 時刻訂正後の原文維持、履歴保存、cursor失効、解析入力の再固定。
- 原文をrestrictedへ変更した後、記憶・context・件数・既存receiptが漏れないこと。
- guest切替後、古い入力packet・private学習・履歴が再利用できないこと。
- context活性だけで、本文に語句一致しない記憶を候補にできること。
- OR条件の重複加算防止と、別surfaceへのmodifier持ち越し防止。
- 大きなturnを部分削除しないこと。
- HTTP境界での認証、旧route不在、エラー本文への入力値の不要な再掲防止。
- 署名backupの復元、改ざん検出、稼働workspaceへの上書き拒否。
- 明示消去が新しい架空workspaceのRaw・staging・ローカルbackupを含むこと。

ブラウザ確認では、架空の庭に関する4発話だけを用いた。原文4件＋テスト代替器の派生記憶1件の表示、等間隔「推定」の表示、直近2turnと非表示、日本語検索、context想起、検索選択／最終入力の別表示、guest切替時の0件化・入力欄消去を確認した。画面の閲覧者表示も、HTML初期値ではなく現在のDB状態から取得する。

テスト実行時には既存のStarlette／httpxの非推奨警告が出るが、試験失敗はない。今回、既存環境の依存パッケージ更新は行っていない。

## 実モデル評価について

以前指定された接続先の`/v1/models`に対する接続確認は、接続拒否になった。モデルへ架空データを送る評価は実施できていない。実ログを用いた再試行や、別providerへの切替は行っていない。

通常のmodel adapterは実装済みだが、自動試験では明示的なテスト代替器とHTTP応答の模擬を使っている。これは意味要約の品質合格を示さない。

再開用に`scripts/evaluate_v1_fictional.py`を追加した。入力はリポジトリ内の架空fixtureに固定し、任意の実ログや旧DBを入力する引数を持たせていない。新workspace、接続先、model名、容量、report出力先を明示する。失敗時に別modelへ切り替えず、意味的な品質は別途review待ちと記録する。

接続が復旧した後の評価では、次を構造合格とは別に判定する。

1. 要約が中間削除ではなく、話題と出来事の意味を保持するか。
2. ユーザー発言とassistant発言の帰属が正しいか。
3. 決定・意図・未解決事項が取り違えられないか。
4. 単発の印象を一般化や真偽確定へ昇格させないか。
5. 文脈所属が原文根拠と整合し、無関係な記憶を過剰に想起しないか。
6. 入力増加量、遅延、出力切れ、再試行率、実モデルの利用状況。

## 未完了の工程と次の順序

計画の全工程を完成扱いにせず、以下を残件として固定する。

1. **ライブ収録と確定境界**：受信時刻／応答完了時刻、streaming中turn、tool完了、日次Uの確定を新hostと接続する。Raw append／manifestを大規模運用に適した保存方式へ拡張する。
2. **日次jobと再処理worker**：実行時刻・retry・再開・時刻訂正queueの消化、後継派生物の版管理を完成する。既存schedulerへfallbackしない。
3. **P4の残件**：前回活性の時間減衰、context倫理の合成、非内容的な対話style、一般化review・Hebbian更新、新hostの全入力経路での認可再検証。
4. **新UI・Observatory**：カレンダー、manual context／運用操作のUI、観測feed、新Observatory接続。旧API互換bridgeを作らない。
5. **復旧・消去の運用拡張**：新規発話を失わない復旧差分の保全・合流、複数ownerの部分消去、外部保持コピーの扱い。
6. **性能と全システム受入**：1万／10万／100万発話でp50／p95、メモリ、保存量、索引更新、学習投入・再分類・検索を測定する。現状のフルスキャンや世代コピーのままで大規模性能合格とはしない。
7. **実モデル品質評価**：接続復旧後、架空データのみで上記品質を確認する。

これらが完了するまでP6は未完了、P7実ログ初期学習は未開放とする。既存DBの移行を後工程へ先送りしているわけではなく、既存DBの内容移行そのものが今回の計画に含まれていない。

## 参照先

- 開発者向けの契約・起動手順・工程別対応表：`v1/README.ja.md`
- 実装：`v1/`
- 試験：`tests/test_v1_*.py`
- 架空fixture：`tests/fixtures/v1/fictional_garden.json`
- 架空データの実モデル評価入口：`scripts/evaluate_v1_fictional.py`
