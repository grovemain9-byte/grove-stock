---
name: swarm-research
description: 問いを分解して並列sonnet subagentで多角調査し、矛盾検出・synthesis・lessons還流まで行うリサーチloop
when_to_use: 戦略検証・OSS評価・バグ根因調査など、複数の視点を同時に当てて統合したい調査タスク
---

# swarm-research

## Description

リサーチ問いを角度ごとに分解し、sonnet subagentを並列で走らせて多角調査する。
一subagent一角度の原則で冗長を排除。矛盾検出と出典付きsynthesisが必須出力。

## Trigger

- 「調べて」「リサーチして」「原因を調査して」
- 技術選定・OSS評価（`tasks/todo.md` P2のOSS精査など）
- 実測エッジの根拠収集・戦略案の裏付け調査

## Steps

1. **問いの分解**
   - 調査テーマを角度別のサブ問い3-5個に分解
   - 各サブ問いに担当角度を明示: コード実測 / ドキュメント / web / 過去ログ（build_diary/lessons）
   - 角度が重複するサブ問いはマージしてから並列へ

2. **並列subagent調査**（モデル: sonnet）
   - 一subagent一角度。同じ角度を複数に割り当てない
   - 各subagentへの指示に「grove-stock PJ外のファイルに触るな」を明記
   - 調査対象パス例: `/Users/ryu/grove-stock/src/` / `docs/grove-stock/` / `tasks/lessons.md`
   - コード実測角度: 実際にコマンド実行して数値を取る（コードを読んでPASSは禁止）

3. **矛盾検出**
   - 全subagent報告を突合し、食い違い（数値差・方向性の逆転・前提の不一致）を列挙
   - 矛盾ありの場合: 当該角度のsubagentに再調査を依頼してから4へ
   - 矛盾なしの場合: 4へ直行

4. **Synthesis（出典付き）**
   - 問いへの回答を3-5行でまとめる
   - 各主張に出典（ファイル:行 or コマンド出力）を付ける
   - 仮説と実測済み事実を明確に分離して記載

5. **還流**
   - 新たに判明したルール・失敗パターン → `/Users/ryu/grove-stock/tasks/lessons.md` に追記
   - 対応アクションが明確な場合 → `/Users/ryu/grove-stock/tasks/todo.md` に追記（形式遵守）
   - `idea-rubric.md` のEdge Evidence軸の更新材料があれば明示してGroveへ

## Ultra Settings

- subagent数上限: 5（それ以上の角度はマージまたは優先度で絞る）
- 敵対検証が必要な場合（合意が重要な判断）: opusを1体追加して adversarial probe
- 調査結果がGrove専権（kill_criterion判定/LIVE判断）に触れる場合 → 材料整理+推奨のみ出力、判定要求はしない
- GO/KILL判断の材料を出す場合は `/Users/ryu/grove-stock/product-taste.md` のGrove専権事項を確認してから
- 安全境界 `/Users/ryu/grove-stock/CLAUDE.md` の「安全境界」節を常時遵守
