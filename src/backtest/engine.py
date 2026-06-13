"""BNF式バックテストエンジン。
キャッシュ済み日足データに対して P1-P5 を走らせ、エントリ/エグジット再現。

出力指標: 総trades, 勝率, 平均リターン, Sharpe, 最大DD, exit_reason内訳, 月次PnL
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Optional

import duckdb
import numpy as np
import pandas as pd

from src.data.cache import CACHE_DB, load_universe, load_bars
from src.data.db import calc_commission
from config.strategy_params import DEFAULTS


def _trade_cost_pct(entry_price: float, raw_pnl: float, position_size: float,
                    slippage_bps: float, apply_commission: bool) -> float:
    """S7: 1取引の往復コストを position_size 比で返す（既定0=無コスト）。

    realistic_baseline._round_trip_cost_pct と同一定義（engineは測定層を
    import しない方向のため最小複製。立花手数料=calc_commission）。
    """
    if entry_price <= 0 or (slippage_bps <= 0 and not apply_commission):
        return 0.0
    cost = 0.0
    if apply_commission:
        shares = position_size / entry_price
        exit_price = entry_price * (1.0 + raw_pnl)
        cost += (calc_commission(entry_price, shares)
                 + calc_commission(exit_price, shares)) / position_size
    if slippage_bps > 0:
        s = slippage_bps / 10_000.0
        cost += (1.0 + raw_pnl) - (1.0 + raw_pnl) * (1.0 - s) / (1.0 + s)
    return cost

logger = logging.getLogger("backtest.engine")

# 戦略定数: S2 で単一ソース DEFAULTS を参照（値は現値と同一＝挙動保存）。
# module名は据え置き（runner 等が `from engine import MA_PERIOD` 等で参照）。
MA_PERIOD = DEFAULTS.ma_period
RSI_PERIOD = DEFAULTS.rsi_period
BB_PERIOD = DEFAULTS.bb_period
BB_STD = DEFAULTS.bb_std
RSI_THRESHOLD = DEFAULTS.rsi_threshold
CONSENSUS_MIN = DEFAULTS.consensus_min
STOP_LOSS = DEFAULTS.stop_loss
MAX_HOLD_DAYS = DEFAULTS.max_hold_days
NIKKEI_CODE_YF = "^N225"  # 非戦略・fallback用・据え置き

# セクター閾値デフォルト (閾値辞書に無い場合)
DEFAULT_SECTOR_TH = DEFAULTS.default_sector_th


@dataclass
class Trade:
    code: str
    entry_date: date
    entry_price: float
    exit_date: Optional[date] = None
    exit_price: Optional[float] = None
    exit_reason: Optional[str] = None
    consensus_at_entry: int = 0
    pnl_pct: Optional[float] = None
    hold_days: Optional[int] = None
    # 2-I: entry時のregimeとsize倍率（regime-conditional sizing）。既定=フラット(1.0)で従来挙動。
    entry_regime: Optional[str] = None
    size_mult: float = 1.0


@dataclass
class BacktestResult:
    trades: list[Trade] = field(default_factory=list)
    params: dict = field(default_factory=dict)
    equity_curve: pd.Series = field(default_factory=lambda: pd.Series(dtype=float))

    def metrics(self) -> dict:
        if not self.trades:
            return {"total_trades": 0}
        closed = [t for t in self.trades if t.pnl_pct is not None]
        if not closed:
            return {"total_trades": len(self.trades), "closed": 0}
        pnls = np.array([t.pnl_pct for t in closed])
        wins = pnls > 0
        m = {
            "total_trades": len(self.trades),
            "closed": len(closed),
            "wins": int(wins.sum()),
            "losses": int((~wins).sum()),
            "win_rate": float(wins.mean()),
            "avg_return": float(pnls.mean()),
            "median_return": float(np.median(pnls)),
            "best": float(pnls.max()),
            "worst": float(pnls.min()),
            "std": float(pnls.std(ddof=1)) if len(pnls) > 1 else 0.0,
        }
        # Sharpe (per-trade basis, annualize by avg trades/year)
        if m["std"] > 0:
            m["sharpe_per_trade"] = m["avg_return"] / m["std"]
        # exit_reason breakdown
        reasons = {}
        for t in closed:
            reasons[t.exit_reason] = reasons.get(t.exit_reason, 0) + 1
        m["exit_reasons"] = reasons
        # Max DD on equity curve
        if len(self.equity_curve) > 0:
            peak = self.equity_curve.cummax()
            dd = (self.equity_curve - peak) / peak
            m["max_drawdown"] = float(dd.min())
        return m


def _compute_indicators(df: pd.DataFrame) -> pd.DataFrame:
    """前処理: close, MA25, RSI14, BB下限, 出来高トレンド, EMA900傾き。
    df must have columns: date, open, high, low, close, volume"""
    d = df.sort_values("date").reset_index(drop=True).copy()
    d["ma25"] = d["close"].rolling(MA_PERIOD).mean()
    d["deviation"] = (d["close"] - d["ma25"]) / d["ma25"]
    # RSI
    delta = d["close"].diff()
    gain = delta.clip(lower=0).rolling(RSI_PERIOD).mean()
    loss = -delta.clip(upper=0).rolling(RSI_PERIOD).mean()
    rs = gain / loss.replace(0, np.nan)
    d["rsi"] = 100 - (100 / (1 + rs))
    # BB lower
    d["bb_mean"] = d["close"].rolling(BB_PERIOD).mean()
    d["bb_std"] = d["close"].rolling(BB_PERIOD).std()
    d["bb_lower"] = d["bb_mean"] - BB_STD * d["bb_std"]
    # Volume trend (past 3d decreasing)
    d["vol_decreasing"] = (d["volume"].diff(1) < 0) & (d["volume"].diff(2) < 0)
    # 2026-05-20 本田MTG bridge: EMA900 + 20日傾き (per_stock_uptrend_required=True 時のみ参照)
    # ewm は O(n)。データ短い時は NaN のまま伝播 → 警戒期 hard-fail で捕捉。
    d["ema900"] = d["close"].ewm(span=900, adjust=False, min_periods=900).mean()
    d["ema900_slope_up"] = d["ema900"] > d["ema900"].shift(20)
    return d


def _load_nikkei_bars(end_date: Optional[str] = None) -> pd.DataFrame:
    """日経225の MA25 乖離を返す。DuckDB cache (nikkei_bars) を優先、cache miss なら yfinance fallback。

    Args:
        end_date: ISO-8601 ("YYYY-MM-DD"). 指定時は cache をその日以前で切る。
            未指定なら全期間。characterization test の reproducibility 確保用。

    cache が空 or end_date より cache が古い時のみ yfinance を叩く（moving window 依存）。
    """
    from src.data.cache import load_nikkei_bars as _load_cache

    df = _load_cache(end_date=end_date)
    cache_too_old = False
    if end_date and not df.empty:
        cache_max = pd.Timestamp(df["date"].max())
        target = pd.Timestamp(end_date) - pd.Timedelta(days=14)
        cache_too_old = cache_max < target
    if df.empty or cache_too_old:
        # cache miss or 古すぎる → yfinance fallback (moving window・reproducibility なし)
        import yfinance as yf
        t = yf.Ticker("^N225")
        raw = t.history(period="5y")
        if raw is None or raw.empty:
            return pd.DataFrame()
        df = raw.reset_index()[["Date", "Close"]].rename(columns={"Date": "date", "Close": "close"})
        df["date"] = pd.to_datetime(df["date"]).dt.tz_localize(None).dt.date
        if end_date:
            df = df[df["date"] <= pd.Timestamp(end_date).date()]
    df = df.dropna(subset=["close"]).sort_values("date").reset_index(drop=True)
    df["ma25"] = df["close"].rolling(MA_PERIOD).mean()
    df["nikkei_dev"] = (df["close"] - df["ma25"]) / df["ma25"]
    return df[["date", "nikkei_dev"]].dropna()


def run_backtest(
    *,
    sector_thresholds: dict[str, float],
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    max_concurrent_positions: int = 10,
    initial_balance: float = 1_000_000.0,
    position_size: float = 100_000.0,  # 1 position あたり10万円相当
    news_negatives: Optional[pd.DataFrame] = None,  # Phase 1-B: (ticker4, filing_date)
    allowed_regimes: Optional[set] = None,  # W: {'bearish','ranging'} 等で絞り込み
    stop_loss: float = STOP_LOSS,
    max_hold_days: int = MAX_HOLD_DAYS,
    consensus_min: int = CONSENSUS_MIN,
    p4_required: bool = True,  # 2026-05-12: False で P4 を強制条件から外す（consensus 集計には含む）
    p4_threshold: float = 0.0,  # P4 の発火閾値 nk_dev < p4_threshold (default 0.0)
    slippage_bps: float = 0.0,       # S7 opt-in: 片道slippage(bps)。既定0=従来挙動(golden exact)
    apply_commission: bool = False,  # S7 opt-in: 立花往復手数料を控除。既定False=従来挙動
    rank_candidates: bool = False,   # ①opt-in: 枠超候補を辞書順でなく原理優先順で選別。既定False=従来挙動exact
    take_profit_dev: float = 0.0,    # 2026-05-20 本田MTG bridge: 利確閾値 deviation>=take_profit_dev（既定0=従来挙動exact）
    per_stock_uptrend_required: bool = False,  # 2026-05-20 本田MTG bridge: EMA900上昇時のみエントリ可（既定False=従来挙動exact）
    # 2-I regime-conditional sizing（研究instrument・default-off）。
    # ⚠️ LEVERAGE CAVEAT: entry gate は建玉数チェックのみで現金/証拠金制約が無い(下記 entry check 参照)。
    # multiplier>1 は展開資本を initial_balance 超(最大 max_concurrent×mult×100%)に膨らませる=暗黙レバレッジ。
    # 変種間の calmar 比較は等展開に正規化するか cash-gate を足してから。maxDD は margin call を見ていない。
    # 詳細: docs/grove-stock/research/2-I-tail-gate.md (S1 gate RED, defer 2026-06-14)
    regime_size_multipliers: Optional[dict] = None,  # 2-I: entry-regime別 size倍率 {'bearish':2.0,...}。既定None=フラット(従来exact)
    bearish_max_concurrent: Optional[int] = None,    # 2-I tail-cap: 弱気entryの同時保有上限。既定None=共通上限のみ(従来exact)
) -> BacktestResult:
    """ユニバース全銘柄を対象にPortfolio-level バックテスト。

    P4は日経MA25乖離 < 0 で発火（regime filter）。
    エントリ条件: consensus ≥ 3 かつ 既存ポジション数 < max_concurrent_positions。
    エグジット: stop_loss -5% / MA25回帰 / 5営業日 / consensus < 3 のいずれか。
    """
    universe = load_universe()
    if universe.empty:
        raise RuntimeError("universe empty. run cache build first.")

    # 日経MA25乖離 (indexをpd.Timestamp(normalize)に揃える)
    # end_date 指定時は cache を切って固定 window で取得（reproducibility 確保）
    nikkei = _load_nikkei_bars(end_date=end_date)
    nikkei["date"] = pd.to_datetime(nikkei["date"])
    nikkei = nikkei.set_index("date")

    # 各銘柄の指標を事前計算してメモリに保持
    # 2026-05-20 本田MTG bridge: EMA900上昇ゲート有効時はwarmup確保のため
    # 履歴を full ロード（start_date無視）→ 指標計算 → 取引窓に truncate
    _load_start = None if per_stock_uptrend_required else start_date
    _trade_start_ts = pd.Timestamp(start_date) if (per_stock_uptrend_required and start_date) else None
    logger.info("preparing indicators for %d codes...", len(universe))
    bar_cache: dict[str, pd.DataFrame] = {}
    for _, r in universe.iterrows():
        df = load_bars(r["Code"], start=_load_start, end=end_date)
        if len(df) < MA_PERIOD + 5:
            continue
        df = _compute_indicators(df)
        if _trade_start_ts is not None:
            df = df[pd.to_datetime(df["date"]) >= _trade_start_ts].reset_index(drop=True)
            if len(df) == 0:
                continue
        bar_cache[r["Code"]] = df

    logger.info("prepared %d bar series", len(bar_cache))
    if not bar_cache:
        return BacktestResult()

    # 2026-05-20 本田MTG bridge: EMA900上昇ゲート有効時の warmup honesty 検証。
    # 取引窓の初日に有効な ema900_slope_up が存在しない銘柄が大半 → 警戒期不足で hard-fail。
    # honda_strategy_wf.py:51-52,117-120 と同パターン（trace-before-alarm）。
    if per_stock_uptrend_required:
        # NOTE: ema900_slope_up は `ema900 > ema900.shift(20)` の比較結果で、pandas は
        # NaN同士の比較を False に解決する。よって警戒期中の行は False (NaN ではない)
        # で埋まり、`ema900_slope_up.notna().any()` は常に True を返してしまう。
        # ema900 自体の NaN を見ることで「警戒期不足」を正しく検出する。
        insufficient = []
        for code, df in bar_cache.items():
            valid_slope = df["ema900"].notna()
            if not valid_slope.any():
                insufficient.append(code)
        if len(insufficient) >= len(bar_cache) * 0.5:
            raise RuntimeError(
                f"insufficient_warmup: {len(insufficient)}/{len(bar_cache)} codes "
                f"have no valid ema900 in trade window (need ~900 pre-history bars). "
                f"5yr cache covers ~Y3 only. Sample missing: {insufficient[:3]}"
            )

    # 全日付集合
    all_dates = sorted(set(d for df in bar_cache.values() for d in df["date"]))
    logger.info("backtest date range: %s to %s (%d days)", all_dates[0], all_dates[-1], len(all_dates))

    sector_map = dict(zip(universe["Code"], universe["S33Nm"]))

    # Phase 1-B: negative開示インデックス {ticker4: set(Timestamp)}
    neg_idx: dict[str, set] = {}
    if news_negatives is not None and len(news_negatives) > 0:
        for _, nr in news_negatives.iterrows():
            t4 = nr["ticker4"]
            fd = pd.Timestamp(nr["filing_date"]).normalize()
            neg_idx.setdefault(t4, set()).add(fd)

    open_positions: dict[str, Trade] = {}
    closed_trades: list[Trade] = []
    equity_records = []
    balance = initial_balance

    # レジーム判定用データ (ret_20d 計算のためフル版Nikkei読み込み)
    nikkei_full = nikkei.copy()
    if "ret_20d" not in nikkei_full.columns:
        nikkei_full["ret_20d"] = nikkei_full["nikkei_dev"].rolling(20).mean()  # 近似
    # 本来は close price の pct_change(20) だが close はない。近似として MA乖離の20日平均を使用
    # 正確なレジーム: bearish if dev<=-3% or ret_20d<=-5% else bullish if dev>=3% else ranging

    for today in all_dates:
        today_ts = pd.Timestamp(today).normalize()
        nk_dev = nikkei.loc[today_ts, "nikkei_dev"] if today_ts in nikkei.index else None
        p4_pass = (nk_dev is not None) and (nk_dev < p4_threshold)
        # レジーム判定 (allowed_regimes / regime_size_multipliers / bearish_max_concurrent で参照)
        regime = None
        if nk_dev is not None:
            if nk_dev >= 0.03:
                regime = "bullish"
            elif nk_dev <= -0.03:
                regime = "bearish"
            else:
                regime = "ranging"
        # entry許可 (allowed_regimes指定時のみ絞り込み。未指定なら従来どおり常時許可)
        if allowed_regimes is not None and regime is not None:
            _allow_entry = regime in allowed_regimes
        else:
            _allow_entry = True
        # 2-I: 当日entryのsize倍率（entry-regime別）。regime_size_multipliers未指定=1.0(従来)
        _size_mult = (regime_size_multipliers.get(regime, 1.0)
                      if regime_size_multipliers else 1.0)

        # === exit check ===
        to_close = []
        for code, trade in open_positions.items():
            df = bar_cache.get(code)
            if df is None:
                continue
            row = df[df["date"] == today]
            if row.empty:
                continue
            row = row.iloc[0]
            cur_price = row["close"]
            hold = len([d for d in df["date"] if trade.entry_date <= d <= today]) - 1
            pnl = (cur_price - trade.entry_price) / trade.entry_price
            reason = None
            # Phase 1-B: ⓪ news_negative 最優先 (entry_date以降に負ニュース検出)
            t4 = code[:4]
            if t4 in neg_idx and any(trade.entry_date <= nd <= today for nd in neg_idx[t4]):
                reason = "news_negative"
            elif pnl <= stop_loss:
                reason = "stop_loss"
            elif hold >= max_hold_days:
                reason = "max_hold"
            elif not pd.isna(row["deviation"]) and row["deviation"] >= take_profit_dev:
                reason = "take_profit"
            if reason:
                trade.exit_date = today
                trade.exit_price = float(cur_price)
                trade.exit_reason = reason
                trade.pnl_pct = float(pnl)
                trade.hold_days = hold
                to_close.append(code)
        for code in to_close:
            t = open_positions.pop(code)
            # S7 opt-in: 往復コストを純pnlに反映（既定0=従来挙動 exact）
            cost_pct = _trade_cost_pct(t.entry_price, t.pnl_pct, position_size * t.size_mult,
                                       slippage_bps, apply_commission)
            if cost_pct:
                t.pnl_pct = float(t.pnl_pct - cost_pct)
            closed_trades.append(t)
            balance += position_size * t.size_mult * t.pnl_pct

        # === entry check ===
        if _allow_entry and len(open_positions) < max_concurrent_positions:
            _ranked: list = []  # rank_candidates時の候補バッファ
            for code, df in bar_cache.items():
                if code in open_positions:
                    continue
                row = df[df["date"] == today]
                if row.empty:
                    continue
                row = row.iloc[0]
                if pd.isna(row["ma25"]) or pd.isna(row["rsi"]) or pd.isna(row["bb_lower"]):
                    continue

                sector = sector_map.get(code, "")
                th = sector_thresholds.get(sector, DEFAULT_SECTOR_TH)

                p1 = row["deviation"] <= th
                p2 = row["rsi"] < RSI_THRESHOLD
                p3 = row["close"] < row["bb_lower"]
                p4 = p4_pass
                p5 = bool(row["vol_decreasing"])
                consensus = int(p1) + int(p2) + int(p3) + int(p4) + int(p5)

                # P-F (2026-04-21): P4 (市場レジーム) を必須条件化。
                # bullish regimeでの逆張り暴走を防ぐ。
                # 2026-05-12: p4_required=False で必須条件解除（consensus 集計には p4 含む）
                # 2026-05-20 本田MTG bridge: per_stock_uptrend_required=True なら
                # 銘柄別EMA900上昇 (slope_up True) を追加 AND 条件として要求
                entry_ok = (
                    consensus >= consensus_min
                    and (p4 or not p4_required)
                    and (not per_stock_uptrend_required or bool(row.get("ema900_slope_up", False)))
                )
                if not entry_ok:
                    continue
                # 2-I tail-cap: 弱気entryの同時保有上限（既存弱気建玉が上限なら新規skip）
                if (bearish_max_concurrent is not None and regime == "bearish"
                        and sum(1 for t in open_positions.values()
                                if t.entry_regime == "bearish") >= bearish_max_concurrent):
                    continue
                if rank_candidates:
                    # ①ランク選別: 当日全候補を集め原理優先順で上位を採用。
                    # 流動性=売買代金value（無ければvolume）。tuned重み無し＝
                    # lexicographic(consensus降→乖離深→流動性高)で過学習回避。
                    _v = row["value"] if "value" in row.index else None
                    liq = float(_v) if _v is not None and not pd.isna(_v) \
                        else float(row["volume"]) if not pd.isna(row.get("volume")) else 0.0
                    _ranked.append((int(consensus), -float(row["deviation"]),
                                    liq, code, float(row["close"])))
                else:
                    # 従来: 即時建て・辞書順・max到達でbreak（golden exact保存）
                    open_positions[code] = Trade(
                        code=code,
                        entry_date=today,
                        entry_price=float(row["close"]),
                        consensus_at_entry=consensus,
                        entry_regime=regime,
                        size_mult=_size_mult,
                    )
                    if len(open_positions) >= max_concurrent_positions:
                        break

            if rank_candidates and _ranked:
                slots = max_concurrent_positions - len(open_positions)
                # 原理優先: consensus降 → 乖離深(−dev大) → 流動性高。重み学習しない
                _ranked.sort(key=lambda x: (x[0], x[1], x[2]), reverse=True)
                _bear_open = sum(1 for t in open_positions.values()
                                 if t.entry_regime == "bearish")
                for cons, _ndev, _liq, code, px in _ranked[:max(slots, 0)]:
                    # 2-I tail-cap: 弱気日のランク採用も上限尊重
                    if (bearish_max_concurrent is not None and regime == "bearish"
                            and _bear_open >= bearish_max_concurrent):
                        break
                    open_positions[code] = Trade(
                        code=code, entry_date=today, entry_price=px,
                        consensus_at_entry=cons,
                        entry_regime=regime, size_mult=_size_mult,
                    )
                    if regime == "bearish":
                        _bear_open += 1

        equity_records.append((today, balance + sum(position_size * t.size_mult * ((bar_cache[c][bar_cache[c]["date"]==today]["close"].iloc[0] - t.entry_price)/t.entry_price) for c,t in open_positions.items() if not bar_cache[c][bar_cache[c]["date"]==today].empty)))

    eq = pd.Series({d: b for d,b in equity_records})
    return BacktestResult(trades=closed_trades + list(open_positions.values()), params={
        "sector_thresholds": sector_thresholds,
        "max_concurrent_positions": max_concurrent_positions,
        "position_size": position_size,
        "initial_balance": initial_balance,
    }, equity_curve=eq)
