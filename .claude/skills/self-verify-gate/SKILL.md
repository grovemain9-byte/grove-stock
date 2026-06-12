---
name: self-verify-gate
description: |
  「完了」と言う前に通過必須のゲート。テスト実行・E2E・敵対プローブ・
  Staff-Engineer自問を段階的に行い、self-certを構造的に排除する。
when_to_use: |
  トリガー文言:
    - 「verify」「gate通して」「検証して」「PASSか確認して」
    - 「完了前チェック」「done条件確認」
  条件（自動適用）:
    - コードを実装・修正した直後
    - 「完了」「DONE」「問題ない」と言う直前
    - PRを出す前
    - tasks/todo.md のタスクを閉じる前
---

# Self-Verify Gate

## ゲート構造（全フェーズ順番通り、スキップ禁止）

### Phase 1: テストFIRST確認

```bash
# grove-stock必須コマンド（web3グローバルプラグイン対策でこの形式のみ）
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 .venv/bin/python -m pytest tests/ -q
```

- RED → コードを修正してから再実行（テストを緩めるな）
- 全GREEN → Phase 2へ
- **コードを読んでPASSは禁止。必ず実行せよ**

### Phase 2: E2E self-verify

```bash
python src/main.py --dry-run           # 1サイクル完走確認
python src/broker/tachibana.py --test  # ブローカーデモAPI疎通（設定がある場合）
```
エラー or 異常ログ → Phase 1に戻って修正

### Phase 3: 敵対プローブ（Grill me）

実測値付きで答えよ（「問題ない」のみ=却下）:
1. この実装の最大の弱点は何か？
2. どの境界値・エッジケースで壊れるか？
3. `CLAUDE.md`「安全境界」節に抵触する経路はあるか？
4. `docs/grove-stock/kill_criterion.md` の計測窓と矛盾しないか？

敵対プローブ0件のPASSは却下。最低1件発見して対処or記録。

### Phase 4: Staff-Engineer自問（Would a staff engineer approve this?）

- 既存 `src/` に類似実装はないか / テストがcircular assertionになっていないか
- シークレットが `.env` 以外に書かれていないか
- 市場時間外cronスキップ実装済みか（9:00-11:30 / 12:30-15:30 JST）
- 1 Issue = 1タスクに収まっているか

全チェック通過 → Phase 5へ

### Phase 5: idea-rubric.md採点（実装変更を含む場合のみ）

4共通軸 + BNF固有3軸をスコアリング（根拠1行必須）。
スコアのみ報告禁止。点+根拠+改善ターゲットをセットで出す。

Blast Radius=1（LIVE/実弾/不可侵に触れる）→ **スコア関係なく即Grove確認要求**

## VERDICT形式（全Phase通過後の報告）

```
[VERDICT: PASS / FAIL]
- pytest: <GREEN N件 / RED M件>
- dry-run: <完走 / エラー内容>
- 敵対プローブ: <発見した弱点と対処>
- Staff-Engineer: <チェック結果>
- rubric: <合計点 / 最低スコア軸と改善策>（変更ありの場合のみ）
```

FAIL項目があれば「完了」と言わない。修正してから再ゲート。
