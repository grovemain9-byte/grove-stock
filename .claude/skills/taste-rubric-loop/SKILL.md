---
name: taste-rubric-loop
description: todo.mdのP2案・実測データからidea生成し、idea-rubric.mdの7軸で採点・spec化してtodo.mdに登録するループ
when_to_use: 新機能アイデアや戦略変更案を構造的に評価してtodo.mdへ登録したい時。自動GO/KILLはしない（Grove専権）
---

# taste-rubric-loop

## Description

`idea-rubric.md` の共通4軸+BNF固有3軸でアイデアを採点し、spec化してtodo.mdに登録する評価ループ。
合計点での自動ship/killはしない（rubric運用ルール準拠）。GO/KILL判定は `product-taste.md` → Grove専権。

## Trigger

- 「アイデアを評価して」「rubricで採点して」「新機能を検討して」
- 定期観察の結果から改善案を出したい時
- P2タスクの優先度判断材料が欲しい時

## Steps

1. **対象領域のidea生成（N個）**
   - `/Users/ryu/grove-stock/tasks/todo.md` のP2案と実測データ（build_diary/lessons）を読む
   - 対象ドメイン（戦略変更・実装改善・測定強化等）でidea 3-5個を生成
   - 各ideaを1行で列挙してからLoopへ

2. **7軸採点（各ideaに適用）**
   - `/Users/ryu/grove-stock/idea-rubric.md` を読み、共通4軸+BNF固有3軸で採点
   - 書式: `軸名: N/5 — 根拠1行`（根拠なしのスコアは却下）
   - worker採点後、Leaderが独立採点。乖離≥2の軸は議論してから次へ

3. **上位案のspec化**
   - 採点上位（目安: 全軸平均3以上）の案をspecに落とす
   - spec形式: `目的 / 変更ファイル / DONE条件 / 検証方法`

4. **安全境界チェック（自動）**
   - Blast Radius=1（LIVE/実弾/不可逆）に触れる案 → スコア合計に関わらず**即Grove判断要求**
   - `product-taste.md` のGrove専権事項（LIVE移行/kill_criterion判定/資金変更）に該当 → 即停止
   - paper mode・shadow先行が確保されているか確認

5. **todo.md登録**
   - spec化された案を `/Users/ryu/grove-stock/tasks/todo.md` の適切なPレベルに追記
   - 形式: `- [ ] タスク名 | DONE条件 | 出典（rubric採点日+最高スコア軸）`
   - Blast Radius案は「Grove承認待ち」タグ付きでP2に仮登録

## Ultra Settings

- 対象ideaが多い場合（5+）はsonnet workerに分担採点を委譲、Leaderが統合
- 採点後はGroveへ「判断要求」通知（判断要求/異常/大物完了の3種）。進捗報告はしない
- 安全境界チェックでSTOPが発生した場合は即座にGroveへ実測値テーブル+推奨を送信
