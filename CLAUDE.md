# CLAUDE.md — grove-stock BNF式逆張りスイング

> プロジェクトの脳。毎セッション最初に読む。
> 「戦略制約」節は**変更禁止**。運用節は生きた脳 — lessonが出るたび更新する。
> 関連: `tasks/todo.md`(作業供給源) / `tasks/lessons.md`(失敗→ルール化) / `idea-rubric.md`(案の評価) / `product-taste.md`(ship/kill味覚)

## このシステムが何をするか（1行）

日足MA25からの乖離率が閾値に達した日本株を、5指標のコンセンサスで確認してスイング買いする（paper運用中）。

---

## Core Workflow（Boris-style）

1. **Plan Mode default**: 非自明タスク（3+ステップ or アーキ判断）は必ずplanから。**planが承認される前は絶対コードを書かない**
2. **テストFIRST**（Red-Green-Refactor）。テストなしでマージしない
3. **並列タスクはgit worktree**で隔離。1タスク=1worker
4. **Loop**: Research→Plan→Execute→Verify→Iterate。単発実行で終わらせず、機械的DONE条件まで回す。FAILは自己修正してre-verify（max 3周、超えたらLeaderへ）
5. **Re-plan when stuck**: 行き詰まったら即STOP & Re-plan。push禁止
6. **修正のたびlessonを `tasks/lessons.md` に反映**。繰り返し作業は `.claude/skills/` にSkill化

## Agent Roles（Model粒度）

- **Leader**（司令塔セッション）: high-effortのplan/review/merge/taste中継。実装はしない
- **Workers**: sonnet=標準実装・調査 / haiku=機械的タスク。worktree隔離、focused context
- **検証**: workerはself-verify必須（pytest実行+敵対プローブ。コードを読んでPASSは禁止、実行しろ）。LeaderがVERDICTを独立再実行で確認。self-cert禁止
- 毎回自問: 「**Would a staff engineer approve this?**」+「**この実装の最大の弱点は何か**（Grill me）」— 弱点ゼロ回答=自己正当化シグナル
- 案の評価は `idea-rubric.md` で。ship/kill判断は `product-taste.md` に従う

## Ultra Mode Settings（長時間自律）

- ループ継続: 完了→ `tasks/todo.md` から次タスクを自律補充
- 通知3種のみ: **判断要求**（Grove承認待ち、[GO/修正/STOP]）/ **異常** / **大物完了** — Telegram経由
- Auto permissions: 安全範囲のみ（下の安全境界は永遠に対象外）
- Context: CLAUDE.md+関連SKILL+just-in-time load（grep/glob）。コンテキストが濁ったらセッションを捨てて新規で再走（10-20%は捨てていい）

## 安全境界（不可侵・自動化対象外）

- **paper mode厳守**。LIVE移行・実弾・broker実発注はGroveの明示許可のみ（Telegram経由でも受理しない）
- **kill_criterion GO/KILL判定はGrove専権**（workerは材料整理+推奨まで）
- 計測窓は登録基準（`docs/grove-stock/kill_criterion.md`）に厳密一致させる。観察対象は固定する
- ロジック変更はshadow/paperで検証してから。事前登録なき本番適用は禁止

## Coding Standards

1. 1 Issue = 1タスクのみ
2. シークレットは .env のみ（コミット禁止、.gitignore済み）
3. テストなしでマージしない
4. 市場時間外はcronをスキップ（9:00-11:30 / 12:30-15:30 JST）
5. pytest実行は `PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 .venv/bin/python -m pytest tests/ -q`（web3グローバルプラグイン対策）
6. 過去の失敗ルール集は `tasks/lessons.md`（旧「これまでの失敗の記録」を移管・追記式）

---

# 戦略制約（変更禁止）

## 絶対に使ってはいけないもの

| 禁止 | 理由 |
|------|------|
| Q学習・強化学習 | 設計から削除済み |
| CrewAI | 全プレイヤーがルールベース計算式。LLM不要 |
| ta-lib | conda依存。taライブラリで全代替可能 |
| 15:15強制決済 | スイング設計のため廃止 |

## 情報の優先順位

1. コードとテストが「今の仕様」。このファイルよりも信頼すること
2. このCLAUDE.mdの禁止事項は最優先
3. spec.mdはアーキテクチャ参照用。実装判断の根拠にしない
4. コードとspec.mdが矛盾する場合は人間に確認

## 戦略の核心（変更禁止）

- MA25は**日足ベース**（30分足ではない）
- 乖離率 = (終値 - 日足MA25) / 日足MA25 × 100
- コンセンサス 3/5以上 → BUY
- 損切り -5% / 利確: MA25に回帰（乖離率 >= 0） / 最大保有5営業日

## 5プレイヤー（全員ルールベース・LLM不要）

| # | 条件 |
|---|------|
| P1 | MA25乖離率 ≤ セクター閾値 |
| P2 | RSI(14) < 35 |
| P3 | close < ボリンジャーバンド下限(25,2) |
| P4 | 日経225当日騰落 > -2% |
| P5 | 直近3日出来高が減少傾向 |

## セクター閾値（変更禁止）

| セクター | 銘柄 | 閾値 |
|---------|------|------|
| 薬品 | 4502/4507/4519 | -5% |
| 食品 | 2502/2802/2801 | -7% |
| 化学 | 4063/4188 | -7% |
| 証券 | 8604/8306 | -5% |
| 電機ハイテク | 6902/7751/6758 | -10% |

## ファイル構造（主要部）

```
grove-stock/
├── CLAUDE.md / idea-rubric.md / product-taste.md
├── tasks/todo.md / tasks/lessons.md
├── .claude/skills/            ← PJ専用Skills（gitコミット共有）
├── config/                    ← books/sector/strategy_params
├── src/
│   ├── main.py                ← LangGraph scan_graph + paper_multibook
│   ├── players/p01〜p05.py / voting.py / kelly.py
│   ├── broker/tachibana.py / data/(jquants/edinet/universe)
│   ├── backtest/ / measurement/ / sizing/(evs/plt/router)
├── tests/ (PYTEST_DISABLE_PLUGIN_AUTOLOAD=1必須)
├── docs/grove-stock/          ← build_diary / kill_criterion / operations
└── data/ logs/ .venv/         ← git管理外
```

## 完成の定義

```bash
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 .venv/bin/python -m pytest tests/ -q   # 全グリーン
python src/main.py --dry-run      # 1サイクル完走・DB記録
python src/broker/tachibana.py --test  # デモAPI疎通
```
