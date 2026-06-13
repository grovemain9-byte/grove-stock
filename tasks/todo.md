# tasks/todo.md — 作業供給源（Ultra運転の自律補充はここから取る）

> 形式: `- [ ] タスク | DONE条件 | 出典`。完了は `[x]`+日付。新タスクは発見次第追記。
> P0=今すぐ / P1=今週 / P2=判断・観察待ち。**安全境界(CLAUDE.md)対象はGrove承認が先**。

## 北極星track: ¥2M月利20%（Grove 2026-06-13）

- [x] 2026-06-13 R1 数学的要件分解 | 結論: 現行制約で月20%は構造的困難。条件=勝率70%+実証×配分25%×月15-20回×利幅5%+が全部同時 | 北極星登録
- [x] 2026-06-13 R2 実データマイニング | 最強セル=cons4×1k-3k(n98,78.6%)。¥2M現実シミュ月3.2%。火曜地雷/日曜最強。p1m病巣=シグナル選択 | 北極星登録
- [x] 2026-06-13 R3 戦略空間サーベイ | BNF未実装の本丸=手法スイッチ(レジーム)。追加候補: 五十日<決算ギャップ<新高値<ペア。月20%継続=学術的に1%未満の世界 | 北極星登録
- [ ] **p2m book新設(¥2M専用)** | books.pyにp2m追加(p50m同等シグナル/cons≥4/1k-3k集中/max2-3/火曜skip)+テスト+paper稼働開始 | R1-R3統合判断。W1完了後にworker投入
- [ ] 階段ゲートの事前登録 | 第1階段(月3-5%)→第2(5-10%)→第3(10-20%)の昇格条件をkill_criterion式にGroveと登録 | R統合
- [ ] 手法スイッチ(レジーム検知)設計 | BNF要素B: 地合いで逆張り⇄順張り切替の設計書 | R3。第2階段の本丸
- [ ] 決算ギャップ戦略のshadow設計 | J-Quants earnings_calendar整備+5日保有逆張り(168万件検証で5日保有プラス) | R3。事前登録付きで

## P0

- [x] 2026-06-13 pytest 7 Error根治 | **444 passed**(Leader独立再実行263sで再現確認)。真因=.gitignore巻き込みでsrc/data 7ファイル欠落→ImportError + max_drawdown baseline陳腐化。W1完遂・merge済(8aa7cf2)
- [ ] kill_criterion 6/8最終判定の材料整理 | G1-G4/K1-K5全条件の実測値を登録窓(5/25-6/8)で算出→推奨付きでGroveへ判断要求通知（**判定はGrove専権**）| docs/grove-stock/kill_criterion.md（判定期日超過5日）

## P1

- [ ] weekly_retrain cron設定 | crontab登録+市場時間外スキップ+初回dry-run成功 | build_diary.md 2026-05-21 Next Step
- [ ] test_oss_smoke の max_drawdown baseline更新 | baseline更新+当該テストPASS | build_diary.md（前セッション漏れと明記）
- [ ] docs同期 | operations.md/agent_architecture_plan.mdの記述と現コードの乖離を検出→修正 | S4⑤
- [ ] .env.example の鮮度確認 | 実際に必要な環境変数キーと一致 | S0で発見
- [ ] **¥2M実弾視点のbook別評価** | p1m改造後(5/28〜, 高conviction集中型)のn≥15到達時にwin率/PnL/最大DDを算出→Grove判断要求 | Grove確認2026-06-13(¥2M帯代理=p1m。5book合計PnLは¥2M予測に使わない)

## P2（判断・観察・設計待ち）

- [ ] consensus≥4限定 LIVE A/B の設計案 | 事前登録ドラフト作成→Grove判断要求（BNF base=EDGE_NULL、consensus=4で+1.76%/trade確認済み）| bnf_base_audit_2026-05-20.md
- [ ] regime_filter live A/B 観察継続 | applied=False維持の確認のみ | ab_preregistration.md
- [ ] scipy重み最適化 Phase 1 初回試行 | n+50件到達後に実行 | build_diary.md 2026-05-21
- [ ] OSS 6工程精査（Optuna/stumpy/empyrical/LangGraph） | 各OSSの採用/不採用判定+根拠 | agent_architecture_plan.md L153-160
- [ ] S2/S3エージェント集団のconfig仕様設計 | agent毎model/skill/トリガ定義 | agent_collective_s2_s3_blueprint.md
- [ ] mainブランチへのmerge判断 | kill_criterion判定後にGroveと | 観察期設計
