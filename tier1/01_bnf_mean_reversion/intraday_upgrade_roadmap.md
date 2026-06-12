# BNF Intraday Execution Upgrade — 2ヶ月ロードマップ

作成: 2026-05-12 (Elite Team v2 最終評決後)
オーナー: Davis（実装統括）/ Grove（承認・予算）

## 背景

`scripts/bnf_realistic_backtest.py` + `scripts/bnf_execution_lag.py` の Reality Cascade で:

| 層 | annR | annS |
|----|------|------|
| Raw backtest | +213% | +2.19 |
| + Transaction cost | +119% | +1.22 |
| + 1日execution lag (full) | +91% | +0.98 |
| **+ Lag + OOS** | **+3.5%** | **+0.04** |

→ 真因: T+1 J-Quants データ + cron 9:00 約定 → overnight gap で mean-reversion alpha が消費される。

→ Sharpe 0.04 はノイズ。**現状 BNF は実運用 alpha なし**。

→ 「BNFで利益出したい」を達成するには **intraday execution が唯一の道**。

## 目標

- **Target**: OOS annS 1.0+, annR 40%+ (実装後の paper trade で実証)
- **Hard requirement**: 14:55 信号確定 → 15:00 引成（または 14:58 成行）で同日約定
- **Stretch**: 5-min バー単位の信号更新で intraday MA25 信号も狙う

## Phase 0: Broker API capability ✅ 完了 (2026-05-12)

| 候補 | WebSocket | 引成 | 個人無料 | 環境制約 | 判定 |
|------|-----------|------|----------|----------|------|
| 立花証券 e支店 v4r7+ | ✅ (2025-05追加) | ✅ | ✅ | なし | **採用** |
| kabu STATION API | ✅ (400ms push) | ✅ | プロ枠 | Windows常駐必須 | 不採用 (Mac mini稼働環境) |
| J-Quants Light | ❌ (T+1 only) | N/A | ¥1,650/月 | EOD only | 現状維持 (日足は使う) |

採用: **立花証券 v4r7+ WebSocket API**。既存 `src/broker/tachibana.py` (v4r8 polling) を拡張。

## Phase 1: Realtime data feed POC (Week 1-2)

**Deliverable**:
- `src/realtime/ws_client.py` — WebSocket client (立花 v4r7+)
- `src/realtime/tick_store.py` — Tick → 1min bar 集計 → DuckDB保存
- `tests/test_ws_client.py` — demo環境で 5銘柄 1時間連続接続
- 検証: J-Quants 終値と一致 (±0.5%)

**Decision gate**: realtime feed 1時間連続稼働 + 終値一致 → Phase 2 へ

**Risk**: 立花 v4r7+ WebSocketのdemo環境動作未確認。POC で 1-2日かかる可能性。

## Phase 2: Intraday signal engine (Week 3-4)

**Deliverable**:
- `src/intraday/signal_engine.py` — 日足MA25 + intraday tick → 信号生成
  - 14:55 timer 起動 → 全494銘柄の latest tick + 日足MA25 で deviation 計算
  - P1〜P5 voting (intradayベース)
  - consensus≥3 → 引成 order trigger
- `src/intraday/risk_gates.py` — 同時保有10銘柄上限 + Tiered Kelly sizing
- `tests/test_intraday_signal.py`

**Decision gate**: 1週間 paper運用で daily と intraday シグナルの差を実測

## Phase 3: MOC order routing + integration (Week 5-6)

**Deliverable**:
- `src/broker/tachibana.py` 拡張: `place_moc_order()` (sCondition=引成)
- `src/main_intraday.py` — 14:55 起動の新 entry point
- `tests/test_moc_order.py` — demo環境で MOC約定検証
- 統合 backtest: `src/backtest/intraday_engine.py` (5min bar基準)

**Decision gate**: intraday backtest で OOS annS > 1.0 達成 → Phase 4

## Phase 4: Paper trade live (Week 7-8)

**Deliverable**:
- launchd plist で 14:55 trigger 設定
- 2週間 paper運用 (demo環境)
- 結果: paper annR, paper Sharpe, paper MaxDD を実測
- 比較: paper vs intraday backtest (差 < 30% なら本物)

**Decision gate**: paper Sharpe > 0.8 → Phase 5 (Grove承認後 LIVE)

## Phase 5: LIVE migration (Week 9+, Grove承認後)

- 初期: bankroll 0.1% per trade (極小)
- 2週間で +0.5%/月以上なら 0.5%/trade
- 4週間で MaxDD < -3% 維持なら本格運用

## 即時 (今セッション残り)

- ✅ [計画] roadmap文書 作成 (この文書)
- ⏳ Phase 1 着手前に Grove approval 確認
- ⏳ 今日の changes (consensus 4→3, p4_optional) は **paper として残す** (baseline data accumulation)
- ⏳ CHECKPOINT update

## リスク・前提

| # | 項目 | 確率 | 影響 | 緩和策 |
|---|------|------|------|--------|
| 1 | 立花 WebSocket demo環境で動かない | 中 | 大 | Phase 1 POC で1週目に判明、kabuS-API+Windows VM 移行 |
| 2 | 引成オーダーが ¥1k 以下の小口で約定しない | 中 | 中 | 成行 14:58 で代替、slippage測定 |
| 3 | Intraday backtest Sharpe < 0.5 | 低 | 大 | Phase 3 で判明、戦略pivot (pairs等) |
| 4 | Grove予算: 2ヶ月のDavis稼働 | 低 | 小 | Phase別 go/no-go gate あり、撤退コスト小 |
| 5 | 立花API レート制限 10RPS × 494銘柄 | 低 | 中 | tick押込→1min集計で 1RPS以下、十分余裕 |

## 既存資源

- `src/backtest/engine.py`: グリッド最適化済み (c=3, no_p4, no_regime)
- `src/broker/tachibana.py`: v4r8 polling 実装済 (拡張ベース)
- `data/grove_stock.duckdb`: 19,818 scans 過去データ
- `data/history_cache.duckdb`: 494銘柄 × 3年 日足
- J-Quants Light 契約 (1,650円/月、日足用に継続)

## 出典

- 立花証券 e支店 API v4r7 (WebSocket追加): https://www.e-shiten.jp/api/
- kabu STATION API: https://kabucom.github.io/kabusapi/ptal/
- Realistic backtest evidence: `/tmp/bnf_realistic.json`, `/tmp/bnf_lag.json`
- Elite Team v2 評決根拠: Gemini Researcher analysis (日本株 mean-rev benchmark 10-15% / Sharpe 0.8-1.2)
