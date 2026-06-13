# W2 BRIEF — weekly_retrain cron一式

お前はgrove-stock worker **W2**（独立セッション、branch `task/weekly-retrain`）。CLAUDE.md（安全境界・Coding Standards）を先に読め。

## 目的
build_diary 2026-05-21の宿題「`weekly_retrain` cronの設定」を実装する。

## 手順
1. **調査**: `docs/grove-stock/build_diary.md` で `weekly_retrain` / `retrain` / `scipy重み` の文脈を読み、何をretrainするのか（PLT? 重み最適化? どのスクリプト?）を特定。`scripts/` に既存の関連スクリプトがないか確認
2. **実装**: `scripts/weekly_retrain.sh`（または適切な形）を作成 — 市場時間外スキップ（9:00-11:30/12:30-15:30 JST中は実行しない設計）、ログ出力先 `logs/`、失敗時はexit非0
3. **crontab行は提案まで**: `crontab`コマンドは禁止（権限deny済み）。スクリプト冒頭コメントに推奨crontab行を書く（正時を避けた分を選べ、例 `7 3 * * 6`）。適用はLeaderがやる
4. **dry-run実証**: スクリプトを `--dry-run` 等の安全モードで実際に実行し、完走を証明

## ループ
Plan→Execute→Verify（dry-run実行+関連pytestのみ: `PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 /Users/ryu/grove-stock/.venv/bin/python -m pytest tests/ -q -k retrain` 該当が無ければ全体はW1に任せて構文/dry-runで代替）→自己修正 max 3周。

## 境界
- 戦略制約節変更禁止 / paper mode厳守（retrainがbroker発注に繋がる経路を作るな）
- git push禁止 / crontab・launchctl禁止 / grove-stock外に触るな
- retrainの実体が調査で確定できない場合: 推測実装するな。調査結果+設計提案を `docs/grove-stock/weekly_retrain_proposal.md` に書いてDONE扱い（その旨を最終行に明記）

## DONE（機械的）
1. スクリプト存在+dry-run完走の実証（コマンド+出力）または提案md
2. このbranchにcommit
3. `tasks/lessons.md` に学び追記
4. 最終出力の単独行で `W2 DONE`（失敗なら `W2 BLOCKED: <理由>`）
