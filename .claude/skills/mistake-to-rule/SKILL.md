---
name: mistake-to-rule
description: |
  エラー・想定外発生時に即 tasks/lessons.md へ記録し、繰り返し効くものは
  CLAUDE.md昇格 or 新Skill化する。溜めない・忘れない・繰り返さない。
when_to_use: |
  トリガー文言:
    - 「lessonに記録して」「失敗をrule化して」「これを教訓に」
    - 「また同じミスをしないように」「rulesに昇格して」
  条件（自動適用）:
    - テスト失敗・予期せぬエラーが発生した直後
    - 同じ問題を2回以上踏んだと気づいた時
    - 想定外の挙動・計測値のズレを発見した時
    - セッション末（溜まった教訓を一括記録）
---

# Mistake-to-Rule

## 即時記録ルール（発生したらその場で）

「セッション末にまとめて書く」は禁止。気づいた瞬間に書く。

### Step 1: tasks/lessons.md に1行追記

`tasks/lessons.md` の末尾に追記（既存内容は変えるな）:
```
[YYYY-MM-DD] <カテゴリ>: <何が起きたか> → <今後こうする>
```
カテゴリ例: `pytest` / `計測窓` / `安全境界` / `API` / `broker` / `data` / `strategy`
実例: `[2026-06-13] pytest: web3プラグインが干渉 → PYTEST_DISABLE_PLUGIN_AUTOLOAD=1必須`

### Step 2: 繰り返し判定

同じカテゴリのエントリが `tasks/lessons.md` に 2+ 件ある?
- YES → CLAUDE.md昇格 or Skill化を検討（Step 3へ）
- NO → 記録のみで完了

### Step 3: CLAUDE.md昇格 or Skill化の判断基準

| 条件 | アクション |
|------|-----------|
| 毎セッション踏む可能性がある | CLAUDE.md「Coding Standards」節に追加 |
| 特定フローで繰り返す（3+ステップ手順） | `.claude/skills/` に新Skill提案 |
| 安全境界に関わる | CLAUDE.md「安全境界」節に追加（Grove確認後） |
| grove-stock固有の数値・パス | CLAUDE.md または tasks/lessons.md に留め置く |

**CLAUDE.md「戦略制約（変更禁止）」節への追記はGrove専権。勝手に変えるな。**

### Step 4: 昇格実行

CLAUDE.md運用節への追記: Editツールで最小差分のみ変更（grove-stock外ファイルは触らない）。
新Skill化: `mkdir -p /Users/ryu/grove-stock/.claude/skills/<skill-name>` → SKILL.md作成（80行以内）。
Skill化はGroveへの提案形式で出す。自動作成してからGrove確認でよい。

## セッション末チェック（セッション終了前に実行）

`tasks/lessons.md` を開いて確認:
1. 未記録のエラー・想定外はあったか？ → あれば今すぐStep 1
2. 繰り返しエントリ（2+件同カテゴリ）はあるか？ → あればStep 3で昇格検討
3. `CLAUDE.md`「運用節」に反映すべき新知見はあるか？

実パスリファレンス: `tasks/lessons.md`（追記先）/ `CLAUDE.md`（昇格先・戦略制約節は触らない）
/ `/Users/ryu/grove-stock/.claude/skills/`（Skill作成先）/ `docs/grove-stock/`（大物完了時）
