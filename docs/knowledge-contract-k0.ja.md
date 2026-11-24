# 辞書的記憶のK0契約

2026-09-21 / 契約版 `knowledge.k0.v1`

K0は辞書的記憶の入力、評価、採用条件を固定する工程である。[契約モデル](../v02/knowledge_contracts.py)と[架空データの契約試験](../tests/test_knowledge_contracts.py)を追加した。続く[K1](knowledge-storage-k1.ja.md)で保存・認可・手動reviewのAPIへ接続した。候補の自動抽出はK2、探索と実際の証拠評価はK3、問いへの適用と注入はK4の対象であり、未接続である。

`adoption_decision`は解決済みのsnapshotに対する副作用のない参照判定である。構造化された根拠が条件を満たすかを検査するが、資料が本物か、内容が主張を支持するか、出典が本当に独立かを自力では判定しない。型の検証成功や`eligible=true`を、API認可、真偽の証明、実モデルによる利用成功とは扱わない。

## 1. K0で固定する方針

| 項目 | 固定した契約 |
|---|---|
| 主張の同一性 | 原文、構造化命題、主体・帰属、肯定/否定、量化、条件、適用時点、principal/conversation/context/domain/sandboxの完全一致をfingerprint化する。候補照合用であり、自動merge命令ではない |
| 不明な主張 | 構造化できない欄はnull/unknownで保持可能。空欄同士の一致から同じ命題と推定しない。採用判定では構造の未解決を留保する |
| 候補入力 | `KnowledgeCandidate`は由来、主張、scope、provenanceのみ。重み、assessment、知識状態、旧confirmed、activation、対象不明confidenceを受け付けない |
| 初期状態 | `initial_claim`でprovisional、world_claimのassessmentはunknown/null、反証はunassessed、独立性はunknown。昇格も永続化も行わない |
| 評価対象 | 実装名`assessment`と既存`Assessment`を再利用。記録忠実度・情報源信頼性をworld_claimへ転送しない。`epistemic_assessment`という別更新キーは作らない |
| 根拠profile | 初期版は独立観測、限定的な帰属付き報告、個別直接観測、形式的導出の四種。各profileはpolicy側で条件を固定する |
| 初期scope | 会話・文脈単位、world/fictionを分離。今回、複数会話collectionやvisibility拡張を導入しない |
| 採用期限 | review_afterを過ぎたら通常採用を不可とする。履歴・scoreは変更せず、K4で再確認待ちの留保区画へ出せるようにする |
| 撤回 | 四状態のまま、withdraw判断を正規台帳に残す。再採用は最新の撤回を明示参照したadoptのみ。holdで撤回を解除しない |
| 更新元 | 正規command一つ。K0はDBへ書かない。K1で版・認可・監査・状態の更新を同じtransactionへ接続する |

文字の大小、表記揺れ、同じ時刻の異なる表記もK0では自動正規化しない。このため意味の同じ候補が重複して残ることはあり得る。主張の誤統合を避ける初期仕様であり、同義照合は由来を保つ独立したreviewとして追加できる。

## 2. 型と役割

| 型 | 役割 |
|---|---|
| `KnowledgeScope` | principal、conversation、context、world/fiction、sandbox。fictionとsandboxを対で指定 |
| `Proposition` | 原文、主張種別、subject/predicate/object、polarity、quantifier、attribution_actor_id、conditions、valid_from/until |
| `RecordRef` | オブジェクトIDと不変snapshotの版。同一集合に同じIDの複数版を混ぜない |
| `ClaimTarget` | claim_id、claim_revision、evidence_set_revision。review・evidence・searchの対象を固定 |
| `KnowledgeCandidate` | 通常の暫定候補作成入力 |
| `ClaimSnapshot` | 認可・参照解決後の正規読取形。状態やassessmentを含むが、通常create/updateの入力ではない |
| `EvidenceSnapshot` | 主張に対する支持/反証/限定/派生、内容照合状態、資料の版・digest・抜粋、起源、発言者、検証参照 |
| `SourceIndependence` | 独立性status、根拠IDの組ごとの判定と理由、対象付きprovenance |
| `CounterevidenceSearch` | 探索対象・scope、実行状態、実行した問い、結果参照、coverage、限界、停止理由 |
| `CounterevidenceReview` | 探索結果の意味評価。反証status、対象根拠、解消理由 |
| `KnowledgeReview` | 四状態に対する判断、profile、world_claim assessment、根拠snapshot、反証/独立性評価、policy、再確認期日 |
| `ReviewContext` | サーバーが解決した認可・枝・参照・審査時刻・最新撤回。問いの条件充足を含まない |
| `AdoptionContext` | サーバーが解決した認可・枝・参照・適用条件・最新撤回の情報。クライアントの自己申告を受け付けるAPIではない |
| `AdoptionDecision` | eligibleと不成立理由。入力の値や重みを変更しない |

`source_kind`や`content_check=matched`は認可された評価器の判断を表現する欄であり、クライアントがそう書けば審査済みになる意味ではない。根拠資料は、会話eventだけでなく検索取得物もその由来を持つ正規sourceとしてK1/K2で解決する。URLや検索順位を証拠の品質判定に代用しない。

## 3. 版と本文変更の契約

- `record_version`: 正規保存の競合検証用。review保存などで増える。
- `claim_revision`: 主張本文・適用条件の版。
- `evidence_set_revision`: 審査対象となる根拠集合の版。

採用は後二者とreviewの対象を照合する。review保存によってrecord_versionが増えただけでは失効させない。根拠集合が更新されて旧reviewと一致しなくなった場合は、通常採用を停止して再確認待ちにする。

K1で本文・条件を変更するときは、新claim_revision、provisional、assessment unknown、旧reviewの採用効力無効化を同じtransactionで保存する。過去の本文・評価は不変snapshotとして残す。K0の型を作り直す操作は、この永続更新や競合検査を実装したことにはならない。

review内のevidence、search、counterevidence参照は同じClaimTargetとscopeへ結び付ける。Assessment内の根拠IDはreview内にある版付き根拠へ解決する。reviewの対象主張本文とassessment.claimも一致しなければ採用しない。

## 4. 四状態と採用台帳

| review action | state_after | 採用上の意味 |
|---|---|---|
| `hold` | provisional | 評価待ち・根拠不足。過去の撤回は解除しない |
| `adopt` | established | 別途すべての採用条件を満たした場合だけ通常知識へ使用可能 |
| `dispute` | disputed | 対立を留保して保持 |
| `propose_withdrawal` | withdrawal_candidate | 採用撤回を審査する。通常採用しない |
| `withdraw` | withdrawal_candidate | 採用撤回を台帳に確定。記憶は削除しない |

`Assessment.status=assessed`だけではadoptにならず、state_after=establishedだけでも採用可能にならない。撤回後のadoptには`reopens_withdrawal_ref`で最新の撤回を指定する。ClaimSnapshotとAdoptionContextの最新撤回が違う場合も通常採用を拒否する。

K0では撤回日時とadopt日時も検査する。同じ時刻に記録された操作の厳密な順序は時刻だけでは証明できないため、K1の正規台帳がtransactionの順序と最新の採用/撤回判断を解決する。

## 5. 根拠profileの初期条件

すべてのprofileに、明確な主張、内容照合済みの支持、対象版の一致、標準の反証探索、未解決の重要な反証がないこと、認可・適用範囲の確認を要求する。scoreの大小だけで採用しない。

| profile | 主張と支持の条件 | 限界 |
|---|---|---|
| `independent_observations` | empiricalかつsingle/bounded。内容照合済みの直接観測があり、そのうち二件の異なる起源について明示的な独立性reviewがある | 二つのURLという規則ではない。一般的な全称経験則は初期profileの対象外。より広い主張には別policyが必要 |
| `attributed_report` | attributed_reportかつsingle。主張の帰属先actorと一致する一次報告が支持する | 採用できるのは「その人がそう報告した」という限定的主張。報告内容そのものの世界での真実性や、本人の全期間の性質を証明しない |
| `direct_observation` | bounded_observationかつsingle。既知の起源を持つ、内容照合済みの直接観測 | その観測・条件の範囲に限定。他人・別時点・全事例へ広げない |
| `formal_derivation` | formal。検証参照を伴うchecked_derivationが支持する | 検証器・前提条件・証明対象の妥当性はK3で確認。単なるLLMの「証明できた」は不可 |

後三種で複数の独立起源を要求しない理由は、主張と根拠の構造が異なるためである。独立性unknownをindependentへ書き換える例外ではない。K0の初期profileは網羅的な知識分類ではなく、適用範囲を限定した最初のpolicyである。拡張時はpolicy版とfixtureを追加する。

## 6. 独立性と反証探索

独立性は`unknown / dependent / partially_independent / independent`。明示的にindependentと判断する組では、両根拠の起源が既知で、同じsource IDではなく、origin_idsに共通起源がないことを要求する。別URL・別著者・複数モデルというだけでは独立性を付与しない。

originsが異なることは独立性の十分条件ではない。K3で共通一次資料・転載・同一観測・引用関係を調べたうえで、reviewとして独立性を記録する。K0は矛盾した宣言を拒否する検査であり、隠れた依存関係を発見する実装ではない。

| 軸 | 値 |
|---|---|
| 探索job状態 | not_started / planned / running / completed / blocked / deferred |
| 反証評価 | unassessed / none_found_in_scope / potential / material_unresolved / addressed |

completedには実行した問い、coverage、完了時刻を要求する。blocked/deferredには停止理由を要求する。途中で反証を見つけた後にblockedになることは有効であり、発見済みの反証を捨てない。一方、未完了jobを不発見・解消済みの証明には使わない。

通常採用には、当該reviewに組み込んだ標準探索が完了していることを要求する。過去の失敗jobは履歴に残し、完了した新しい探索snapshotを別ID/版で参照する。履歴上の失敗を削除するための規則ではない。

未照合・判定不明のrefutes/qualifiesが残る場合は通常採用を保留する。addressedなら、内容が合致した反証・限定資料の全件を解消reviewの対象に含める。棄却済みのmismatched資料は理由を残して除外できる。支持資料やderived_fromだけを「解消した反証」として参照しない。none_found_in_scopeは対象範囲での不発見であり、真である証明やconfidence増分ではない。

## 7. 通常採用の参照判定

`adoption_decision`は次を検査し、不成立理由を返す。

1. サーバー側の認可、現在枝での使用可否、参照解決、問いに対する条件充足が確認されている。
2. claim、review、使用contextのscopeが一致する。
3. claimの命題版・根拠集合版がreview対象と一致し、latest_review_refが同じである。
4. 状態がestablished、判断がadopt、対象本文に対するworld_claim assessmentがassessedである。
5. assessment、反証・独立性、再確認期日の現在値がreviewのsnapshotと一致する。
6. 最新の撤回が未解除で残っていない。解除する場合はその撤回を明示参照した後続adoptである。
7. 適用時点とreview期日の範囲内であり、根拠・各評価・探索結果がreviewより後の資料ではない。
8. 主張の構造が未解決ではなく、profile別の支持条件と反証条件を満たす。

validityは`valid_from <= applicable_at < valid_until`の半開区間、評価時刻evaluated_atがreview_afterへ到達したら再確認が必要とする。問いが対象とする時点と、今その知識を使う審査時刻を分ける。過去についての質問ではapplicable_atをその過去の時点にできるが、review期限の検査を過去へ巻き戻して通過させてはならない。期限によって物理削除やscoreの再計算は行わない。

K1の`review_decision`は上記の共通審査を、宣言された命題・条件の範囲内で行う。問いの条件充足とapplicable_atの検査は`adoption_decision`に残す。ReviewContextのboolはAPI入力に公開せず、K1が所有権、資料の版、branch、fiction境界、最新撤回を確認して組み立てる。K4ではさらに命題の条件と問いの関係を解決してAdoptionContextを作る。`conditions_satisfied`を単なる検索一致や固定trueで代用せず、確認できない場合はfalseとして留保する。

## 8. 三重み・感情・観測の非結合

知識の契約にはretrieval_weight、generalization_weight、salienceの変更欄を設けない。既存memoryの値を別の更新契約で保持し、候補やreviewに追加された旧キーもextra fieldとして拒否する。

world_claimのscoreは証拠reviewの入力であり、感情・想起回数・出典数からK0が生成しない。反証探索の完了でもscoreを変更しない。profileの条件を満たして知識を採用しても、経験の一般化強度は0/unevaluatedの既存契約を維持する。

K4で作るpacketはepisode_evidence / dictionary_claims / knowledge_caveatsを分け、主張の版とreview参照を含める。selected / compiled / host_finalizedは異なる観測として扱う。K0ではpacketの送信や観測の保存は実装していない。

## 9. K1以降へ渡す実装条件

| 工程 | この契約だけでは満たせない条件 |
|---|---|
| K1（接続済み） | recordsの新kind、不変版履歴、認可、expected_version、冪等性、transaction、監査、backup/restore、明示削除。[保存仕様](knowledge-storage-k1.ja.md)に詳細を定める。ClaimSnapshotをcreate payloadとして公開しない |
| K2 | 原文・資料からの候補抽出、原本の保存、起源の追跡、明示migration。旧semantic/confirmedをそのまま確立へ変換しない |
| K3 | 主張と証拠の内容照合、根拠種別の判定、反証探索、独立性判定、解消理由の妥当性、評価器の校正。profileを候補の自己申告だけで選ばない |
| K4 | 問いへの適用条件照合、UID解決、三つの注入区画、版の固定、selected/compiled/host_finalizedの観測 |
| K5/K6 | shadowと実モデル評価、明示migration・設定切替。K0の試験成功を稼働機能の完成としない |

検証は架空の報告・観測・形式的主張だけを用いる。実データ、外部モデル、検索connectorを使わず、肯定例と誤採用を拒否する例を対照にする。実行例は`python -m unittest discover -s tests -p "test_knowledge_contracts.py" -v`。
