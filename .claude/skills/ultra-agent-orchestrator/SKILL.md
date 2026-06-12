---
name: ultra-agent-orchestrator
description: |
  Leader(高effort計画/review) + 並列Worker(sonnet/haiku実行) + Verify loopを
  grove-stock BNF式ワークフローに配線したオーケストレーションパターン。
when_to_use: |
  トリガー文言:
    - 「team mode」「チームで回せ」「orchestrate」「並列で走らせて」
    - 「workerに分けて」「Leader-Worker構成で」
  条件: 3+タスクが独立分解可能 or 1タスクが5+ファイル変更を伴う
---

# Ultra Agent Orchestrator

## Model粒度（変更禁止）

| 役割 | モデル | 担当 |
|------|--------|------|
| Leader | 司令塔(high-effort) | plan/review/merge/taste判定 |
| Worker標準 | sonnet | 調査・実装・テスト |
| Worker機械 | haiku | grep/ファイル生成・整形 |
| 敵対検証 | opus | 弱点検出・adversarial probe |

## Steps

### Step 1: Leader — 詳細Plan + タスク分解

```
1. CLAUDE.md + tasks/todo.md を読み、対象タスクを1行で定義
2. 分解: 独立サブタスク N 本をリストアップ（依存関係明示）
3. 各サブタスクに「Worker種別 / 入力 / 完了条件」を付与
4. 安全境界チェック: CLAUDE.md「安全境界（不可侵）」節に抵触しないか確認
   → paper mode / broker実発注 / LIVE移行 に触れるならGrove確認要求で即STOP
5. planをGroveに提示してGO承認を受けてから次へ（コード書き禁止）
```

### Step 2: 並列Worker spawn

```bash
# git worktreeで各Workerを隔離（推奨）
git worktree add .worktrees/task-A -b worker/task-A
git worktree add .worktrees/task-B -b worker/task-B
# 各Workerへ渡す: タスク1行 / 入力ファイルパス / 完了条件(test名) / 安全境界要約
```

### Step 3: Worker実行 + self-verify

```bash
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 .venv/bin/python -m pytest tests/ -q  # 全GREEN必須
python src/main.py --dry-run  # 1サイクル完走確認
```

コードを読んでPASSは禁止。敵対プローブ0件のPASSは却下。「Grill me」自問必須。
FAIL → 自己修正 → re-verify（max 3周。超えたらLeaderへ）

### Step 4: Leader review + merge

1. 各Workerの成果物を独立再実行でVERDICT確認（self-cert禁止）
2. `idea-rubric.md` 4軸でスコアリング（根拠1行必須）
3. 全Worker PASSかつ安全境界クリアでmerge（mainへのmergeはGrove専権）
4. `tasks/lessons.md` に新発見ルールを追記 → `CLAUDE.md` 運用節を更新

### Step 5: ループ継続 or 終了

完了 → `tasks/todo.md` から次タスクを自律補充してStep 1へ。
FAIL 3周超 → LeaderからGroveへ判断要求（判断要求/異常/大物完了の3種形式）。

## Ultra Settings

```
Leader:  高effort・CLAUDE.md+idea-rubric.md+product-taste.mdをフルロード
Workers: 担当タスクのfocused context（関連ファイルのみ）・fast設定
検証:    pytest必須 + opusによる敵対プローブ + Leaderの独立再実行
通知:    3種のみ（判断要求/異常/大物完了）。進捗細報告しない
```

## 安全境界リマインダー（不可侵）
- paper mode厳守。LIVE/broker実発注/実弾はGroveの明示許可のみ
- kill_criterion GO/KILL判定はGrove専権（Workerは材料+推奨まで）
- mainブランチmergeはGrove専権
