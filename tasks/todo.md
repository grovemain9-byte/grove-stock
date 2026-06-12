# tasks/todo.md — 作業供給源（Ultra運転の自律補充はここから取る）

> 形式: `- [ ] タスク | DONE条件 | 出典`。完了は `[x]`+日付。新タスクは発見次第追記。
> P0=今すぐ / P1=今週 / P2=判断・観察待ち。**安全境界(CLAUDE.md)対象はGrove承認が先**。

## P0

- [ ] pytest 7 Error根治 | `PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 .venv/bin/python -m pytest tests/ -q` 全PASS、Leaderの独立再実行で確認 | 実測2026-06-13（48%地点で7E連続）
- [ ] kill_criterion 6/8最終判定の材料整理 | G1-G4/K1-K5全条件の実測値を登録窓(5/25-6/8)で算出→推奨付きでGroveへ判断要求通知（**判定はGrove専権**）| docs/grove-stock/kill_criterion.md（判定期日超過5日）

## P1

- [ ] weekly_retrain cron設定 | crontab登録+市場時間外スキップ+初回dry-run成功 | build_diary.md 2026-05-21 Next Step
- [ ] test_oss_smoke の max_drawdown baseline更新 | baseline更新+当該テストPASS | build_diary.md（前セッション漏れと明記）
- [ ] docs同期 | operations.md/agent_architecture_plan.mdの記述と現コードの乖離を検出→修正 | S4⑤
- [ ] .env.example の鮮度確認 | 実際に必要な環境変数キーと一致 | S0で発見

## P2（判断・観察・設計待ち）

- [ ] consensus≥4限定 LIVE A/B の設計案 | 事前登録ドラフト作成→Grove判断要求（BNF base=EDGE_NULL、consensus=4で+1.76%/trade確認済み）| bnf_base_audit_2026-05-20.md
- [ ] regime_filter live A/B 観察継続 | applied=False維持の確認のみ | ab_preregistration.md
- [ ] scipy重み最適化 Phase 1 初回試行 | n+50件到達後に実行 | build_diary.md 2026-05-21
- [ ] OSS 6工程精査（Optuna/stumpy/empyrical/LangGraph） | 各OSSの採用/不採用判定+根拠 | agent_architecture_plan.md L153-160
- [ ] S2/S3エージェント集団のconfig仕様設計 | agent毎model/skill/トリガ定義 | agent_collective_s2_s3_blueprint.md
- [ ] mainブランチへのmerge判断 | kill_criterion判定後にGroveと | 観察期設計
