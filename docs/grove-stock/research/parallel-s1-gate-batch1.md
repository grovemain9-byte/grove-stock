# 並列S1ゲート バッチ1 — 2-D / 2-H / 3-3 / 2-C（2026-06-14）

## 概要
価格データだけで足りる4モジュールのS1 edge-gate（建てる前に5年backtestでedge実在確認）を
**並列sonnet worker ×4** で同時実行し、生き残り(GREEN)を **opus independent-evaluator** で独立検証。
方法論ガードレール（2-Iの教訓: 年率化・レバレッジ交絡なし・tail込み・apples-to-apples・look-ahead禁止）を全workerに焼いた。

期間 2021-05-19..2026-06-12、universe ~1337-1571銘柄、base position_size=100k、逆張りbaseline参照。

## 結果サマリー
| module | 仮説 | 判定 | 要点 |
|---|---|---|---|
| 2-D 撤退速度 | 速く/厳しく切ると改善 | **RED** | tighter(-3%)≒baseline、faster(3d)わずか、rebound-fail撤退は悪化(ann_Sharpe 0.60<0.77)。逆にlooser(-7%)が最良0.87=仮説と逆 |
| 2-C セクター連れ高 | 強いセクターの出遅れ株が追いつく | **RED** | コストON でedge消滅(ann_Sharpe 0.45→0.10、avg_ret CIゼロ跨ぎ)。逆張りbaseline(0.70)に全指標で劣る。look-ahead排除済 |
| 2-H 利確トレーリング | trailingが固定利確に勝つ | **YELLOW** | DD減は構造的に本物(2024-08 -19%→-14〜16%)。だがリターン上積みはノイズ(CI重複)、勝率57%→43-50%低下。worker自身が「逐次replayが10ポジ同時保有を非再現＝calmar過大」と注記 |
| 3-3 新高値ブレイクアウト | 高値ブレイク順張りに単体edge | **YELLOW**(worker GREEN→opus格下げ) | 下記詳細 |

## 3-3 詳細（opus独立検証で格下げ）
worker報告: Spec A(素朴20日高値+MA25) ann_Sharpe 1.73 vs 逆張り0.84、CAGR+27%、GREEN。
opus監査の発見:
- **Sharpe水増し**: スクリプトの equity は「exit日にPnLをdumpする疑似portfolio」(3-3_breakout_gate.py:299-326)。真のmark-to-market portfolio Sharpe = **1.47**(1.73でない)、真のmaxDD **-21.7%@2021-12-20**(報告-17%@2022-01-06でない)。
- **beta vs alpha**: 日経buy&hold(2021-26) CAGR+18.4%/Sharpe0.89。戦略 beta=0.45/corr=0.58/alpha≈+16%。純betaではない（2-Iより実体ある残差alpha）が、過半が強気地合いと連動。
- **GREEN自体がartifact**: スクリプトの2.0x基準は「水増し候補Sharpe 1.73 vs 正直なbaseline 0.84」を比較。like-for-like(真MTM)だと 1.47/0.84 = **1.74x で2.0x未達**。
- **常時フル投資**: 1261/1323営業日が満玉(max=7)、95%が長期ロング。7枠は独立でない＝相関ベットをi.i.d.扱い。
- **survivorship bias**: universe_snapshotは現時点スナップのみ(時点別履歴なし)。delisted銘柄を含まず alpha が上方バイアス。
- look-ahead: CLEAN(.shift(1)で当日close不使用)。
→ **needs-portfolio-resim**: 真MTM curve + like-for-like 2x基準 + 時点別universe + regime分割 で再ゲートしてから建築判断。

## 教訓
- **並列S1ゲートが機能**: 4本同時に建てる前で篩い分け、2 RED即kill、2 YELLOWは精査対象に。junkは1つも建築段階に進まず。
- **exit-dump疑似Sharpeの罠**: per-trade PnLをexit日にdumpした系列でSharpeを取ると、lumpyな分散で過大化。真のmark-to-market curveで測れ。3-3でも2-Hでも同型。
- **見かけGREENはopus監査必須**: workerの自動GREEN(自分のSharpeをbaselineのMTM Sharpeと比較)は apples-to-oranges。independent-evaluatorがlike-for-likeで格下げ。
- **全モジュールのmaxDDが2024-08駆動**: 5年で大暴落1回(しかも急反発)＝tail検証の構造的限界。bear/range regimeがサンプルに無い。
- **worker forkの不完全終了**: 2-D workerが自分のbackground backtest結果を回収せずターン消尽→commanderがスクリプト回収実行。fork時は「結果回収まで」をbriefに含めるべき（lesson）。

## 状態更新
- 2-D ⬜→❌RED / 2-C ⬜→❌RED / 2-H 🟡→🟡(trailing=DD防御として保持価値、利確edgeはnull) / 3-3 🔵→🟡(needs-portfolio-resim)

## scripts（再現）
- `docs/grove-stock/research/2d_exit_gate.py` / `2h_trailing_gate.py` / `3-3_breakout_gate.py` / `2c_sector_laggard_gate.py`
- 実行: `cd /Users/ryu/grove-stock && PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 PYTHONPATH=. .venv/bin/python <script>`
