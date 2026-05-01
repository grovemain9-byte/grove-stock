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

logger = logging.getLogger("backtest.engine")

# 戦略定数 (CLAUDE.md準拠)
MA_PERIOD = 25
RSI_PERIOD = 14
BB_PERIOD = 25
BB_STD = 2.0
RSI_THRESHOLD = 35
CONSENSUS_MIN = 4  # 2026-04-21: 3→4 (厳選で勝率向上狙い)
STOP_LOSS = -0.07  # 2026-05-02: 令和式 -5%→-7% (grid best: Sharpe 0.250 +19%)
MAX_HOLD_DAYS = 15  # 2026-05-02: 令和式 10→15 (grid best: bearish_only+stop=-7%)
NIKKEI_CODE_YF = "^N225"  # fallback用

# セクター閾値デフォルト (閾値辞書に無い場合)
DEFAULT_SECTOR_TH = -0.07


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
    """前処理: close, MA25, RSI14, BB下限, 出来高トレンド。
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
    return d


def _load_nikkei_bars() -> pd.DataFrame:
    """日経225の履歴をyfinanceで取得（J-Quants Lightはindices未対応）。MA25乖離列付きで返す。"""
    import yfinance as yf
    t = yf.Ticker("^N225")
    df = t.history(period="5y")
    if df is None or df.empty:
        return pd.DataFrame()
    df = df.reset_index()[["Date","Close"]].rename(columns={"Date":"date","Close":"close"})
    df["date"] = pd.to_datetime(df["date"]).dt.tz_localize(None).dt.date
    df["ma25"] = df["close"].rolling(MA_PERIOD).mean()
    df["nikkei_dev"] = (df["close"] - df["ma25"]) / df["ma25"]
    return df[["date","nikkei_dev"]].dropna()


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
    nikkei = _load_nikkei_bars()
    nikkei["date"] = pd.to_datetime(nikkei["date"])
    nikkei = nikkei.set_index("date")

    # 各銘柄の指標を事前計算してメモリに保持
    logger.info("preparing indicators for %d codes...", len(universe))
    bar_cache: dict[str, pd.DataFrame] = {}
    for _, r in universe.iterrows():
        df = load_bars(r["Code"], start=start_date, end=end_date)
        if len(df) < MA_PERIOD + 5:
            continue
        bar_cache[r["Code"]] = _compute_indicators(df)

    logger.info("prepared %d bar series", len(bar_cache))
    if not bar_cache:
        return BacktestResult()

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
        p4_pass = (nk_dev is not None) and (nk_dev < 0)
        # レジーム判定 (allowed_regimes指定時)
        if allowed_regimes is not None and nk_dev is not None:
            if nk_dev >= 0.03:
                regime = "bullish"
            elif nk_dev <= -0.03:
                regime = "bearish"
            else:
                regime = "ranging"
            if regime not in allowed_regimes:
                # エントリ禁止日（既存positionのexitのみ継続）
                _allow_entry = False
            else:
                _allow_entry = True
        else:
            _allow_entry = True

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
            elif not pd.isna(row["deviation"]) and row["deviation"] >= 0:
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
            closed_trades.append(t)
            balance += position_size * t.pnl_pct

        # === entry check ===
        if _allow_entry and len(open_positions) < max_concurrent_positions:
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
                if consensus >= consensus_min and p4:
                    open_positions[code] = Trade(
                        code=code,
                        entry_date=today,
                        entry_price=float(row["close"]),
                        consensus_at_entry=consensus,
                    )
                    if len(open_positions) >= max_concurrent_positions:
                        break

        equity_records.append((today, balance + sum(position_size * ((bar_cache[c][bar_cache[c]["date"]==today]["close"].iloc[0] - t.entry_price)/t.entry_price) for c,t in open_positions.items() if not bar_cache[c][bar_cache[c]["date"]==today].empty)))

    eq = pd.Series({d: b for d,b in equity_records})
    return BacktestResult(trades=closed_trades + list(open_positions.values()), params={
        "sector_thresholds": sector_thresholds,
        "max_concurrent_positions": max_concurrent_positions,
        "position_size": position_size,
        "initial_balance": initial_balance,
    }, equity_curve=eq)
