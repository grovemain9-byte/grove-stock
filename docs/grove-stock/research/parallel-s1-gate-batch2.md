# 並列S1ゲート バッチ2 — 3-5 ペアトレード / 3-6 空売り対称化（2026-06-14）

## 概要
batch1 と同じ手順（並列sonnet worker → 正直なedge測定 → opus独立検証）。
batch1の監査教訓を強化ガードレールに追加: **真のmark-to-market portfolio Sharpe必須**(exit-dump疑似Sharpe禁止)、
**worker は自分のbackground結果を回収してからverdict**(2-Dのfork失敗対策)、**look-ahead回避を明示**。
両モジュールとも long-short で市場中立的＝3-3を悩ませたbeta交絡が構造的に出にくいのが利点。

## 結果サマリー
| module | 仮説 | 判定 | 要点 |
|---|---|---|---|
| 3-5 ペアトレード | 相関ペアのスプレッドが平均回帰=市場中立edge | **RED** | 真MTM Sharpe -0.018(cost-off)、avg/trade +0.06% CIゼロ跨ぎ(非有意)、cost-ONで-5.8%/年。β-0.009で中立は確認も中立≠edge。stop率25%=2021-23選定ペアが2024-26で崩壊(BOJ利上げで銀行株相関破壊)。look-ahead回避(train/test分割+rolling z)正しく実装 |
| 3-6 空売り対称化 | 逆張りの鏡像(買われ過ぎ空売り)に対称alpha | **RED** | 全regimeで損(avg -0.92〜-1.29%/trade)、CAGR -93.5%、maxDD -100%、WR43% vs 長期57%。bearishが最悪(買われ過ぎ株は弱気でも相対強さで上昇継続)。borrow realism無視・survivorshipは短期に有利でもなお-93% |

## 重要な発見: 逆張りedgeは非対称
- 買い(売られ過ぎ→反発)は効く(baseline Sharpe 0.75)が、**売り(買われ過ぎ→さらに上昇)は効かない**。
- 理由: 2021-26は構造的強気相場。買われ過ぎ株は反転せず上昇継続。3-3(順張りが効いた)と同じ「単一bull regimeが全てを彩る」テーマの裏面。
- ペアトレードも同regime変化(BOJ正常化)で相関が壊れて失敗。

## 教訓
- **逆張りの短期mirrorは成立しない(bull regime下)**: long edgeをそのまま符号反転してもedgeにならない。市場の非対称性(下は急・上は緩)+ regime が効く。
- **Sharpe mirage(新artifact)**: equity が単調減少(資本がbleed)だと、日次pct_changeが微小+平均わずか正でも MTM Sharpe が技術的に正になり得るが**CAGR -93%と併存=無意味**。Sharpeは必ず CAGR/total-return と突き合わせて解釈せよ(3-6 workerが自己捕捉)。exit-dump疑似Sharpe(batch1)に続く2つ目の計測罠。
- **市場中立(β≈0)は edge ではない**: ペアは完璧に中立(β-0.009)だが Sharpe ~0。中立性とalphaを混同するな。
- **in-sample cointegration ≠ out-of-sample mean-reversion**: 2021-23で選んだペアが2024-26のregime変化で崩壊。ペアトレードの古典的罠。

## 状態更新
- 3-5 🔵→❌RED / 3-6 🔵→❌RED

## scripts（再現）
- `docs/grove-stock/research/3-5_pairs_gate.py` / `3-6_short_mirror_gate.py`
- 実行: `cd /Users/ryu/grove-stock && PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 PYTHONPATH=. .venv/bin/python <script>`

## 2バッチ通算の地形（重要）
今日ゲートした全モジュール(2-B/2-I/2-D/2-C/2-H/3-3/3-5/3-6)で、**clean GREEN はゼロ**。
- 唯一の有望lead = 3-3 新高値順張り(実α~+16%だが要portfolio再sim)。
- 2-H trailing = DD防御として価値(攻めはnull)。
- それ以外は全て RED/defer。
- **既存の長期逆張りbaseline(Sharpe 0.75)が、何にもクリーンに負けなかった最強の戦略**。多くのedge候補は2021-26の単一bull regimeのartifact。
- 含意: 新規edge探索より、(a)3-3の決着、(b)既存baselineのsizing/exit精緻化(2-H DD防御)、(c)新データ(決算)バッチ、が次の現実的フロンティア。
