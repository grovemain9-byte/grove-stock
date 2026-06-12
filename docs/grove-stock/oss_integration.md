# OSS統合精査記録（plan S4 / feedback_oss_analysis_methodology 6工程）

engine.py は唯一の戦略/バックテスト真実源。OSS は engine の**出力に対する
後処理（計測・可視化）**に限定し、シグナル生成・約定再現・出口判定には一切
入れない。OSS は `src/measurement/` `src/research/` にのみ常駐。

---

## empyrical-reloaded==0.5.12 （採用・2026-05-16）

| 工程 | 結果 |
|---|---|
| ①実在確認 | PyPI `empyrical-reloaded` 0.5.12（最新, 2025-06-01）。import名 `empyrical`。Stefan Jansen（zipline-reloaded保守者）。**無印 empyrical は2020停止＝採用禁止** |
| ②RCEスキャン | sdist=pyproject.toml+setup.cfg（旧式 setup スクリプト無＝インストール時コード実行なし）。build-system=setuptools宣言的。src/ に OS実行・サブプロセス・動的評価・ネットワーク・直列化ロード・ファイル書込 系の危険API **皆無**（純計算ライブラリ）。`pip-audit`: **No known vulnerabilities found** |
| ③ソース読 | `max_drawdown(returns)` = `nanmin(drawdown_series(returns))` 純numpy。sharpe/sortino/calmar/annual_return も period定数ベースの純計算。**returns系列入力**（engineはequity直計算だが drawdown定義は同一: min((V−peak)/peak)） |
| ④弱点抽出 | (a) 推移的依存に `peewee`(ORM)/`bottleneck` 混入。**stats関数のみ使用しデータ取得系 `empyrical.utils` を import しない**（peewee/web経路を踏まない）。(b) 年率換算は period=DAILY/252前提。日本株252営業日と整合だが annualization 明示推奨。(c) returns入力なので equity→pct_change 変換が必要。(d) 単精度誤差は大規模で出うる→厳密等価でなく許容誤差比較 |
| ⑤Markdown化 | 本ファイル |
| ⑥Phase入力 | `requirements-research.txt`（本体.venv論理分離）+ `tests/test_oss_smoke.py`（empyrical.max_drawdown == engine equity式maxDD のクロスチェック＝整合証明、engineは真実源で温存） |

**温存ガード**: engine.py / players / voting / monitor / kelly に empyrical を
import しない。S5 の `src/measurement/walk_forward.py` でのみ「追加レンズ」
として Sharpe/Sortino/Calmar/maxDD を併記（engine.metrics() と並記、置換不可）。

---

## 後送り（G2/G6: データ充足後フェーズ）

実 positions が3件しかない現状で特徴量マイニング/別backtest基盤を入れるのは
過学習製造機（敵対的レビュー G6）。決着数百件・複数レジームが貯まるまで凍結:

- **stumpy**（matrix profile）/ **tsfresh**（特徴量自動生成）: データ不足で
  spurious。S5計測でデータが貯まってから6工程
- **vectorbt**: engine.py を置換しない。検算用途のみ。fair-code ライセンス
  （Apache2.0+Commons Clause）要確認 → 採用時に6工程
- **optuna**: 凍結設計の「再利用◎」は誤認（G1）。少数paramは既存
  `scripts/bnf_grid_*.py` のグリッド探索で代替評価を先行。広探索が要ると
  判明した場合のみ6工程後に採用。RL/Stable-Baselines3 は CLAUDE.md 禁止
