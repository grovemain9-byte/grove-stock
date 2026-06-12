---
name: dynamic-swarm
description: todo.mdからタスク束を取得しworktree×N並列で走らせ、Leaderが巡回→review→mergeする大規模オーケストレーション
when_to_use: 複数の独立したP0/P1タスクを並列で一気に進めたい時。初期は3-10並列で安定確認してからスケール
---

# dynamic-swarm

## Description

`tasks/todo.md` のタスク群をworktree隔離×N並列で実行するオーケストレーションloop。
**運用判断**: 初期は3-10並列で崩れない運転を確立してからスケール（一気に20並列は禁止）。
10-20%のセッションは途中放棄・再走の前提で設計する。

## Trigger

- 「並列で進めて」「一気にやって」「swarmで走らせて」
- P0/P1の独立タスクが3個以上ある時
- Ultra運転中にLeaderが次バッチを自律補充する時

## Steps

1. **タスク束取得**
   - `/Users/ryu/grove-stock/tasks/todo.md` からP0→P1の順で未完了タスクを取得
   - 独立性チェック: 相互依存するタスクはシリアル化。並列に回すのは独立タスクのみ
   - 安全境界チェック: Grove専権（LIVE/kill判定/merge/資金変更）は取り出さない
   - 初回: 3-5タスクから開始。安定確認後に次バッチへ

2. **worktree×N隔離**
   - 各タスクに専用worktreeを作成: `git worktree add /tmp/grove-worker-{N} -b task/{N}`
   - workerには `CLAUDE.md` + 対象タスクのcontext（just-in-time load）を渡す
   - grove-stock外のファイルに触れることを明示禁止にしてworkerに伝える

3. **workerループ（各worker内蔵、max 3周）**
   - モデル: sonnet=標準実装・調査 / haiku=機械的タスク（ファイル操作・フォーマット等）
   - 各周: Plan → Execute → Verify（`PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 .venv/bin/python -m pytest tests/ -q`）
   - Verify PASS → 機械的DONEフラグを出力してLeaderへ通知
   - 3周失敗 → STOPしてLeaderへ「判断要求」エスカレーション（自己修正は3周が上限）

4. **Leader巡回・review・merge**
   - モデル: 司令塔セッション（Leader）
   - 各workerの出力を独立再実行で確認（self-cert禁止。コードを読んでPASSは禁止）
   - review観点: `idea-rubric.md` のBlast Radius / Staff-Engineer Approval
   - VERDICT OK → worktreeからmain候補ブランチへmerge（**mainへの実際のmergeはGrove専権**）
   - VERDICT NG → 差し戻し or 破棄。セッション破棄は「10-20%は捨てていい」設計

5. **完了通知+次バッチ自律補充**
   - 大物完了時のみGroveへ通知（判断要求/異常/大物完了の3種）
   - todo.mdの完了タスクに `[x] + 日付` を記録
   - 次バッチ: todo.mdから再取得して1へ戻る。Grove判断待ちタスクはスキップ

## Ultra Settings

- worktree並列上限: 初期10、安定確認後に最大20（崩れたら即縮退）
- コンテキスト汚染対策: workerの中間結果はjsonl出力で隔離。Leaderのmainコンテキストに流し込まない
- 敵対検証が必要なreview: opusを1体追加（Leaderの独立判断の補助）
- 安全境界は `/Users/ryu/grove-stock/CLAUDE.md` + `/Users/ryu/grove-stock/product-taste.md` を常時遵守
- paper mode厳守: broker実発注・LIVE移行タスクは取り出し禁止。workerに渡すな
