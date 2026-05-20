# BNF base F+G audit 結果 + kill-gate + consensus-tighten 事前登録

> 2026-05-20 / status: ACTIVE / applied=False（Shadow厳守）
> 作成根拠: 本田MTG bridge 実装後の F+G 監査で BNF base が統計的に EDGE_NULL
> と判定された。本書は (1) F+G の verdict を凍結、(2) BNF default の
> 「kill-gate」を事前登録、(3) F副産物（consensus stratification calibration）
> から派生する次手 LIVE A/B を事前登録する。

---

## 1. F+G audit 確定結果（後付け解釈禁止）

### F: BNF base G3 統計的有意性

scripts/fg_audit.py 実行（既存 engine 流用・新backtestせず・事後分析のみ）。

| 検定 | 値 | 閾値 | 結果 |
|---|---|---|---|
| N (G3 closed trades, 3窓計) | 503 | - | - |
| 平均/trade | +0.60% | - | - |
| Std/trade | 9.31% | - | - |
| Sharpe/trade | 0.0645 | - | - |
| **Bootstrap 95% CI (Sharpe, 10000 resample)** | **[-0.024, +0.148]** | **>0** | **ゼロを含む** |
| **PSR (Probabilistic Sharpe Ratio)** | **0.932** | **>0.95** | fail |
| **DSR (Deflated Sharpe Ratio, k=6 hypotheses)** | **0.576** | **>0.95** | **大幅 fail** |

**Verdict: `EDGE_NULL`** — コスト後のBNF base は標準的閾値（Lopez de Prado）
でノイズと区別不可能。

### F 副産物: 信号強度 (consensus) calibration

| consensus_at_entry | N | 平均/trade | 勝率 | Sharpe/trade |
|---|---|---|---|---|
| 3 | 388 (77%) | +0.07% | 54.1% | 0.009 (ノイズ) |
| **4** | **109 (22%)** | **+1.76%** | **55.0%** | **0.145 (edge あり)** |
| 5 | 6 (1%) | +13.76% | 83.3% | 0.750 (sample 不足だが強) |

**信号強度→期待リターンの単調 calibration が存在**。BNF base が null なのは
「consensus=3 の 77% trades が edge≈0 で全体を希薄化」している構造。

### G: Honda bridge regime-stratified Δ（vs G3 同regimeのbase）

| 候補 | bullish Δ | ranging Δ | bearish Δ | verdict |
|---|---|---|---|---|
| `extended_hold` | **+0.48%** | **+0.34%** | -0.96% | H_mixed (bullish/ranging有・bear損) |
| `asymmetric_tp` | -0.10% | +0.02% | **-1.36%** | H_mixed (全regime勝率-5~-7%) |
| `ema900_uptrend` | **+0.42%** | -0.83% | -0.96% | H_regime_bullish_only_artifact |

**Verdict**: Honda bridge 3候補とも uniform-improvement なし。`ema900_uptrend`
は予測通りの pure regime artifact。`extended_hold` は bullish/ranging で機能・
bear で被害。`asymmetric_tp` は単純な勝ち捨て悪手。

---

## 2. BNF base default kill-gate（事前登録 = B option）

### 仮説（凍結）

**BNF base default（consensus_min=3）は cost-aware で実 edge を持たない**。
F audit が Bootstrap CI/PSR/DSR 3指標で同方向に null を示し、Honda bridge G
analysis が「null base に対する独立lift なし」を補強。

### kill 判定ゲート（後付け解釈禁止）

| 判定 | 条件（全て事前固定） |
|------|------|
| **KILL (BNF default 棄却)** | 以下のいずれかが満たされた瞬間: <br> (a) 次回 F audit 再走（cache 増分後・最低 +50 trades 蓄積後）で **PSR < 0.95 が継続** <br> (b) live A/B regime_filter が ab_preregistration §3 の KILL 条件発火 <br> (c) consensus-tighten LIVE A/B (§3) が **GO 到達せず Y2 trial period 終了** |
| **PASS (継続観測)** | F audit の PSR/DSR が改善トレンド & live A/B regime_filter が PASS 継続 |
| **REVIVE (default 再生)** | F audit PSR > 0.95 **かつ** DSR > 0.95 を 連続 2 回満たす (cache拡大 OR 規律強化での edge 復活) |

### kill 後の運用（事前定義）

- `consensus_min` DEFAULT を 3→4 に flip するには **別途 hypothesis_loop で
  consensus_4 が 3/3 GO を再達成** が必要（現状 PASS 2/3・F audit で意味更新は
  あるが GO verdict ではない）。
- DEFAULTS は触らない（discipline_apply.py Guardian の single-active-invariant
  維持）。kill verdict は「default は edge を持たないと宣言」する記録であり、
  コード change の trigger ではない。
- KILL 発火後、次の生存 hypothesis（現状 regime_filter のみ）が新 default
  候補 = `regime_filter applied=True` への Grove 承認判断軸が clear になる。

### deadline

- **2026-09-30**（4ヶ月）まで PASS 継続なら自動 KILL 検討トリガ（option E:
  indefinite postpone 防止）。それまでに上記 KILL/REVIVE のいずれかが
  発火しなければ Grove が明示判断（continue/kill/pivot）。

---

## 3. consensus-tighten LIVE A/B 事前登録（= D'' option）

### 仮説（凍結）

F audit が示した「consensus=3 の 77% trades が edge≈0 で全体希薄化」
構造から導く: **consensus_min を 3→4 に flip すれば trade volume は ~22% に
減るが per-trade edge が立ち上がる**。backtest 観測値 (consensus=4: 109 trades,
+1.76%/trade, Sh 0.145) が live でも再現するか検証する。

### 実装デザイン（事前固定）

- **既存5book**: 1M/5M/10M/30M/50M（多bookは flex-sizing 用既存設計）
- **本 A/B の追加**: 50M book を 25M × 2 に分割し、各々を treatment(c=4) /
  control(c=3) として並列稼働。**新book追加でなく既存book 分割で会計clean**。
- **treatment**: `consensus_min=4` book（既存 BOOKS spec に `consensus_min_override`
  field 追加・default は DEFAULTS.consensus_min=3 を継承）。
- **control**: `consensus_min=3` book（現状維持）。
- **共通**: 同一 sector_thresholds・同一 cost・同一 universe・同一 cron 起動。

### 評価ゲート（後付け解釈禁止・ab_preregistration §3 と同 schema）

| 判定 | 条件 |
|------|------|
| **GO (consensus≥4 を新 default 候補に昇格)** | (i) **N_min = treatment closed ≥ 80** 到達 **かつ** (ii) treatment 累積equity − control 累積equity > 0 を **3/3 月次窓 (or 20営業日 rolling)** で満たす **かつ** (iii) treatment の per-trade win率 ≥ 50% を維持 |
| **PASS** | N_min 未到達 / 1〜2/3 窓のみ正 → 観測継続 |
| **KILL** | N_min 到達後に **3/3 窓で負** または treatment win率 < 45% 連続 2 窓 |

### N_min 根拠

- backtest consensus=4 は 3年で 109 trades = **~36/年**（consensus=3 の ~130/年
  の約1/3）。
- N_min = 80 ≈ **2年強の paper 蓄積相当**。consensus=3 の N_min=200 (~1.5年)
  より時間がかかるが、本 A/B は cost-aware で本当に edge があるかの最終証拠。

### deadline

- **2026-11-30**（6ヶ月）までに GO 到達しなければ「consensus-tighten
  thesis も null」と判定し、新方向（factor-based selection / non-MA strategy /
  asset class pivot）を検討開始。

---

## 4. 何を変えない（不可侵リスト）

- `config/strategy_params.py` DEFAULTS — F+G verdict は **interpretation
  update** であり code change の trigger ではない
- `src/measurement/discipline_apply.py` Guardian rules — single-active /
  GO-gated / known-field 全て不可侵
- `docs/grove-stock/ab_preregistration.md`（regime_filter A/B）— 別 hypothesis、
  本書とは独立に時間ブロック中（N_min=200 control 非弱気 closed まで PASS）
- `scripts/honda_strategy_wf.py` — Honda standalone、次MTG input 待ち保留

---

## 5. 改版履歴

- 2026-05-20 初版（F+G audit verdict 凍結・BNF default kill-gate 事前登録・
  consensus-tighten LIVE A/B 事前登録）

---

## 6. 関連

- F+G audit 実装: `scripts/fg_audit.py`
- F+G 生 verdict: `data/fg_audit/audit_result.json`
- F+G 生 trades: `data/fg_audit/trades.csv`
- 本田 standalone（独立保留）: `scripts/honda_strategy_wf.py`
- regime_filter A/B（独立・時間ブロック）: `docs/grove-stock/ab_preregistration.md`
- hypothesis_loop registry: `src/measurement/hypothesis_loop.py:40-50`
