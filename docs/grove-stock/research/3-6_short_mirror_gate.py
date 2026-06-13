"""3-6 空売り対称化 S1ゲート: short-side mirror of the BNF reversal strategy

MODULE: 3-6 空売り対称化 (short-side mirror, 逆張り空売り)
PERIOD: 2021-05-19 to 2026-06-12, ~1572 stocks (Japanese universe)

HYPOTHESIS:
  The reversal edge has a short-side mirror: sell stocks that are extended ABOVE
  MA25 (overbought) and cover on mean-reversion — symmetric alpha, strongest in
  bearish/ranging regimes.

METHOD:
  Mirror signals:
    P1' deviation >= +sector_threshold (overbought above MA25)
    P2' RSI > 65
    P3' close > BB_upper
    P4' Nikkei dev > 0 (bullish market — note: tested as both gate and filter)
    P5' volume decreasing (same as long side)
  Consensus >= 3 → SHORT
  Exit: stop +5% (price rose against short) / MA25回帰 (deviation <= 0) / 5d max hold
  TRUE MARK-TO-MARKET daily equity curve → annualized Sharpe, CAGR, maxDD+date,
  win rate (+CI), regime breakdown. Cost-ON: 10bps slippage round-trip.

METHODOLOGY GUARDRAILS (hard-won from 2-I and 3-3):
  1. TRUE MTM portfolio Sharpe — daily equity curve, each open short valued at daily close
  2. SHORT P&L sign: profit = (entry - exit) / entry (price falls = profit)
  3. Stop-loss on short = price RISING +5% (e.g. short at 100, stop at 105)
  4. Tail-aware: maxDD + driving date. Worst short loss reported.
  5. Regime breakdown: shorts should work best in bearish/ranging
  6. COST: cost-OFF + cost-ON (10bps round-trip slippage)
  7. Survivorship bias note: delisted shorts (great short targets) MISSING → conservative bias

READ-ONLY: does not touch src/ or config/. Output script only.
Reproduce:
  cd /Users/ryu/grove-stock && PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 PYTHONPATH=. \\
    .venv/bin/python docs/grove-stock/research/3-6_short_mirror_gate.py
"""
from __future__ import annotations

import logging
import math
from datetime import date

import numpy as np
import pandas as pd
import duckdb

from src.data.cache import load_universe, load_bars, CACHE_DB
from src.backtest.runner import compute_sector_thresholds_from_cache
from src.backtest.engine import run_backtest, _compute_indicators, _load_nikkei_bars as _engine_load_nikkei

# =====================================================================================
# PARAMETERS (fixed — no curve-fitting)
# =====================================================================================
START = "2021-05-19"
END   = "2026-06-12"
YEARS = (pd.Timestamp(END) - pd.Timestamp(START)).days / 365.25

MAX_CONCURRENT = 10      # match long-reversal baseline
POSITION_SIZE  = 100_000.0
PORT_SIZE      = MAX_CONCURRENT * POSITION_SIZE

# Short-mirror thresholds (symmetric to long-side)
RSI_OB_THRESHOLD  = 65.0    # RSI > 65 = overbought (P2')
# threshold factor k=2.0 same as compute_sector_thresholds_from_cache

# Exit params (mirror of reversal: stop = +5%, cover at MA25回帰)
STOP_LOSS_SHORT   = +0.05   # price RISES 5% above short entry → cut loss
MAX_HOLD_DAYS     = 5       # same as long side

# Nikkei: for P4' we test P4' = nikkei_dev > 0 (bullish market)
# But we also test "all regimes" (P4' disabled) to see pure signal
# cost
SLIPPAGE_BPS = 10.0
COST_RT = 2 * SLIPPAGE_BPS / 10_000   # round-trip

MIN_BARS = 60  # warmup for indicators

logging.disable(logging.WARNING)


# =====================================================================================
# NIKKEI DATA
# =====================================================================================

def _load_nikkei_dev() -> dict:
    """Returns dict: date → nikkei_dev (float), using engine cache loader."""
    df = _engine_load_nikkei(end_date=END)  # returns [date, nikkei_dev] with MA25 computed
    if df.empty:
        return {}
    df["date"] = pd.to_datetime(df["date"]).dt.date
    df = df.dropna(subset=["nikkei_dev"])
    return dict(zip(df["date"], df["nikkei_dev"]))


# =====================================================================================
# REGIME CLASSIFICATION (inline — avoids yfinance dependency in regime.py)
# =====================================================================================

def _classify_regime(nk_dev: float | None) -> str:
    if nk_dev is None or math.isnan(nk_dev):
        return "unknown"
    if nk_dev >= 0.03:
        return "bullish"
    if nk_dev <= -0.03:
        return "bearish"
    return "ranging"


# =====================================================================================
# SHORT-MIRROR SIMULATOR
# =====================================================================================

def simulate_short_mirror(
    *,
    require_p4_bullish: bool = False,  # if True: P4'=nk_dev>0 required to SHORT
    cost_on: bool = False,
) -> pd.DataFrame:
    """Portfolio-level short-mirror backtest.

    P1' deviation >= +threshold (sector-specific, k=2.0)
    P2' RSI > 65
    P3' close > BB_upper
    P4' nk_dev > 0 (bullish market = shorts can work) [optional gate]
    P5' volume decreasing (same as long side)
    consensus >= 3 → SHORT at that day's close
    Exit: +5% stop (price rises) / MA25回帰 (deviation <= 0) / 5d

    Short P&L: profit = (entry_price - exit_price) / entry_price
    """
    universe = load_universe()
    if universe.empty:
        raise RuntimeError("universe empty")

    thresholds = compute_sector_thresholds_from_cache(k=2.0)
    sector_map = dict(zip(universe["Code"], universe["S33Nm"]))

    nk_dev_map = _load_nikkei_dev()

    # Default sector threshold (for stocks not in any named sector)
    from config.strategy_params import DEFAULTS
    default_th = abs(DEFAULTS.default_sector_th)  # positive for overbought threshold

    # Load and compute indicators for all stocks
    bar_cache: dict[str, pd.DataFrame] = {}
    for _, r in universe.iterrows():
        df = load_bars(r["Code"], end=END)
        if len(df) < MIN_BARS:
            continue
        df = _compute_indicators(df)
        # Add BB_upper (mirror: we need upper band; engine only has bb_lower)
        df["bb_upper"] = df["bb_mean"] + 2.0 * df["bb_std"]
        bar_cache[r["Code"]] = df

    # Build date→row lookups (use dict for O(1) access)
    row_by_date: dict[str, dict] = {}
    for code, df in bar_cache.items():
        rbd = {}
        for _, row in df.iterrows():
            d = row["date"]
            if hasattr(d, "date") and callable(d.date):
                d = d.date()
            rbd[d] = row
        row_by_date[code] = rbd

    # Build sorted all_dates from START
    start_dt = pd.Timestamp(START).date()
    all_dates_set: set = set()
    for df in bar_cache.values():
        for d in df["date"]:
            if hasattr(d, "date") and callable(d.date):
                d = d.date()
            all_dates_set.add(d)
    all_dates = sorted(d for d in all_dates_set if d >= start_dt)

    open_positions: dict[str, dict] = {}  # code → {entry_date, entry_price, regime}
    closed: list[dict] = []

    # TRUE MTM equity: track realized P&L accumulated + unrealized at each day
    # balance = realized gains accumulated (starts at 0, normalized by PORT_SIZE)
    realized_balance = 0.0
    daily_equity: list[tuple] = []  # (date, portfolio_total_value)

    for today in all_dates:
        nk_dev = nk_dev_map.get(today)
        regime = _classify_regime(nk_dev)

        # P4': nikkei bullish condition for short
        p4_prime = (nk_dev is not None) and (nk_dev > 0)

        # ---- EXIT check ----
        to_close = []
        for code, pos in open_positions.items():
            row = row_by_date[code].get(today)
            if row is None:
                continue
            cur = float(row["close"])
            entry_px = pos["entry_price"]
            dev = float(row["deviation"]) if not pd.isna(row["deviation"]) else None

            # Short P&L sign: profit = (entry - exit) / entry
            pnl_short = (entry_px - cur) / entry_px

            # Hold days (count bars since entry inclusive of today, minus entry day)
            df = bar_cache[code]
            hold = int(sum(1 for d in df["date"]
                          if (d.date() if hasattr(d, "date") and callable(d.date) else d) >= pos["entry_date"]
                          and (d.date() if hasattr(d, "date") and callable(d.date) else d) <= today)) - 1

            # Worst-case: price rose from entry (short loss)
            price_rise_pct = (cur - entry_px) / entry_px

            reason = None
            # Stop: price ROSE +5% above entry (unbounded tail risk for shorts)
            if price_rise_pct >= STOP_LOSS_SHORT:
                reason = "stop_loss"
            # Cover: mean reversion achieved (deviation <= 0, price ≤ MA25)
            elif dev is not None and dev <= 0.0:
                reason = "mean_revert"
            # Max hold
            elif hold >= MAX_HOLD_DAYS:
                reason = "max_hold"

            if reason:
                rt_pnl = pnl_short
                if cost_on:
                    rt_pnl -= COST_RT
                closed.append({
                    "code": code,
                    "entry": pos["entry_date"],
                    "exit": today,
                    "pnl_pct": float(rt_pnl),
                    "reason": reason,
                    "hold_days": max(hold, 0),
                    "regime": pos["regime"],
                    "worst_short_loss": max(0.0, price_rise_pct),  # max adverse excursion
                })
                # Accumulate realized gain into balance
                realized_balance += rt_pnl * POSITION_SIZE
                to_close.append(code)
        for code in to_close:
            open_positions.pop(code)

        # ---- ENTRY check ----
        if len(open_positions) < MAX_CONCURRENT:
            for code in bar_cache:
                if code in open_positions:
                    continue
                row = row_by_date[code].get(today)
                if row is None:
                    continue
                if pd.isna(row.get("ma25")) or pd.isna(row.get("rsi")) or pd.isna(row.get("bb_upper")):
                    continue

                sector = sector_map.get(code, "")
                # Use positive threshold (mirror: deviation >= +threshold for overbought)
                th_raw = thresholds.get(sector, -default_th)
                th_pos = abs(th_raw)  # make positive: overbought threshold

                dev = float(row["deviation"])
                rsi = float(row["rsi"])
                close = float(row["close"])
                bb_upper = float(row["bb_upper"])

                p1_prime = dev >= th_pos           # overbought above MA25
                p2_prime = rsi > RSI_OB_THRESHOLD  # RSI > 65
                p3_prime = close > bb_upper         # above BB upper
                p4_val   = p4_prime                 # nikkei bullish
                p5_prime = bool(row.get("vol_decreasing", False))  # volume decreasing

                consensus = int(p1_prime) + int(p2_prime) + int(p3_prime) + int(p4_val) + int(p5_prime)

                if require_p4_bullish and not p4_prime:
                    continue

                if consensus >= 3:
                    open_positions[code] = {
                        "entry_date": today,
                        "entry_price": float(row["close"]),
                        "regime": regime,
                    }
                    if len(open_positions) >= MAX_CONCURRENT:
                        break

        # ---- TRUE MTM equity ----
        # Portfolio value = PORT_SIZE (initial) + realized gains + unrealized P&L of open shorts
        unrealized = 0.0
        for code, pos in open_positions.items():
            row = row_by_date[code].get(today)
            if row is None:
                continue
            cur = float(row["close"])
            # Short unrealized: gain when price falls below entry
            unrealized += (pos["entry_price"] - cur) / pos["entry_price"] * POSITION_SIZE
        portfolio_value = PORT_SIZE + realized_balance + unrealized
        daily_equity.append((today, portfolio_value))

    # Force-close remaining at end of period
    last_day = all_dates[-1]
    for code, pos in open_positions.items():
        row = row_by_date[code].get(last_day)
        if row is None:
            continue
        cur = float(row["close"])
        entry_px = pos["entry_price"]
        pnl_short = (entry_px - cur) / entry_px
        if cost_on:
            pnl_short -= COST_RT
        df = bar_cache[code]
        hold = int(sum(1 for d in df["date"]
                      if (d.date() if hasattr(d, "date") and callable(d.date) else d) >= pos["entry_date"]
                      and (d.date() if hasattr(d, "date") and callable(d.date) else d) <= last_day)) - 1
        closed.append({
            "code": code, "entry": pos["entry_date"], "exit": last_day,
            "pnl_pct": float(pnl_short), "reason": "eod",
            "hold_days": max(hold, 0), "regime": pos["regime"],
            "worst_short_loss": max(0.0, (cur - entry_px) / entry_px),
        })

    tdf = pd.DataFrame(closed)
    # Attach daily_equity for MTM Sharpe calculation
    tdf.attrs["daily_equity"] = daily_equity
    return tdf


# =====================================================================================
# TRUE MTM EQUITY CURVE & METRICS
# =====================================================================================

def _build_true_mtm_equity(daily_equity: list[tuple]) -> pd.Series:
    """Build true MTM equity curve.

    daily_equity: list of (date, portfolio_value_in_yen)
    Each entry is the TOTAL portfolio value (PORT_SIZE + realized + unrealized) at end-of-day.
    Returns: pd.Series indexed by date, portfolio value.
    """
    if not daily_equity:
        return pd.Series(dtype=float)

    dates, values = zip(*daily_equity)
    eq_series = pd.Series(list(values), index=pd.to_datetime(list(dates)))

    # Reindex to business days (forward fill for non-trading days)
    all_biz = pd.bdate_range(start=START, end=END, freq="B")
    eq_series = eq_series.reindex(all_biz, method="ffill")
    # Fill any remaining NaN at start with PORT_SIZE
    eq_series = eq_series.fillna(PORT_SIZE)
    eq_series = eq_series.clip(lower=1.0)
    return eq_series


def _build_exit_dump_equity(tdf: pd.DataFrame) -> pd.Series:
    """Alternative (approximate): dump P&L on exit day (for cross-check).
    Note: this was the 'pseudo-Sharpe' issue identified in 3-3. Use only for comparison.
    """
    if tdf.empty:
        return pd.Series(dtype=float)
    tdf2 = tdf.copy()
    tdf2["exit_dt"] = pd.to_datetime(tdf2["exit"])
    tdf2["gain"] = tdf2["pnl_pct"] * POSITION_SIZE
    daily_pnl = tdf2.groupby("exit_dt")["gain"].sum()
    all_biz = pd.bdate_range(start=START, end=END, freq="B")
    pnl_r = daily_pnl.reindex(all_biz, fill_value=0.0)
    cum_eq = PORT_SIZE + pnl_r.cumsum()
    cum_eq = cum_eq.clip(lower=1.0)
    return cum_eq


def _equity_metrics(cum_eq: pd.Series) -> dict:
    """Compute CAGR, annualized Sharpe, maxDD from cumulative equity series."""
    if len(cum_eq) < 2:
        return {"cagr": 0.0, "ann_sharpe": 0.0, "max_dd": 0.0, "max_dd_date": "N/A",
                "total_return": 0.0, "calmar": float("nan")}

    total_ret = float(cum_eq.iloc[-1] / cum_eq.iloc[0] - 1.0)
    cagr = float((cum_eq.iloc[-1] / cum_eq.iloc[0]) ** (1.0 / YEARS) - 1.0)

    peak = cum_eq.cummax()
    dd = (cum_eq - peak) / peak
    max_dd = float(dd.min())
    max_dd_idx = dd.idxmin()
    max_dd_date = str(max_dd_idx.date()) if hasattr(max_dd_idx, "date") else str(max_dd_idx)

    daily_ret = cum_eq.pct_change().dropna()
    ann_sharpe = 0.0
    if daily_ret.std() > 0:
        ann_sharpe = float((daily_ret.mean() / daily_ret.std()) * math.sqrt(252))

    calmar = cagr / abs(max_dd) if max_dd < 0 else float("nan")

    return {
        "total_return": round(total_ret, 4),
        "cagr": round(cagr, 4),
        "ann_sharpe": round(ann_sharpe, 3),
        "max_dd": round(max_dd, 4),
        "max_dd_date": max_dd_date,
        "calmar": round(calmar, 3),
    }


# =====================================================================================
# CI HELPERS
# =====================================================================================

def _wilson_ci(k: int, n: int, z: float = 1.96) -> tuple[float, float]:
    if n == 0:
        return (0.0, 0.0)
    p = k / n
    denom = 1 + z * z / n
    center = (p + z * z / (2 * n)) / denom
    half = (z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n))) / denom
    return (center - half, center + half)


def _mean_ci(xs: np.ndarray, z: float = 1.96) -> tuple[float, float]:
    n = len(xs)
    if n < 2:
        m = float(xs.mean()) if n else 0.0
        return (m, m)
    se = xs.std(ddof=1) / math.sqrt(n)
    m = xs.mean()
    return (float(m - z * se), float(m + z * se))


# =====================================================================================
# COMPUTE ALL STATS FOR A TRADE DF
# =====================================================================================

def compute_stats(tdf: pd.DataFrame, label: str, cost_on: bool = False) -> dict:
    if tdf.empty:
        print(f"  [{label}] NO TRADES")
        return {"label": label, "n_trades": 0, "ann_sharpe": 0.0}

    pnls = tdf["pnl_pct"].values
    n = len(pnls)
    wins = int((pnls > 0).sum())
    wr = wins / n
    wr_lo, wr_hi = _wilson_ci(wins, n)
    avg = float(pnls.mean())
    a_lo, a_hi = _mean_ci(pnls)
    median = float(np.median(pnls))
    avg_hold = float(tdf["hold_days"].mean())

    # Worst short loss = max adverse excursion (price rise while short)
    worst_loss = float(tdf["worst_short_loss"].max()) if "worst_short_loss" in tdf.columns else 0.0

    # TRUE MTM equity
    daily_equity = tdf.attrs.get("daily_equity", [])
    cum_eq_mtm = _build_true_mtm_equity(daily_equity)
    eq_stats_mtm = _equity_metrics(cum_eq_mtm)

    # Cross-check with exit-dump (for reference only)
    cum_eq_exit = _build_exit_dump_equity(tdf)
    eq_stats_exit = _equity_metrics(cum_eq_exit)

    return {
        "label": label,
        "cost_on": cost_on,
        "n_trades": n,
        "win_rate": round(wr, 4),
        "wr_ci_lo": round(wr_lo, 4),
        "wr_ci_hi": round(wr_hi, 4),
        "avg_return": round(avg, 5),
        "avg_ret_ci_lo": round(a_lo, 5),
        "avg_ret_ci_hi": round(a_hi, 5),
        "median_return": round(median, 5),
        "avg_hold_days": round(avg_hold, 1),
        "worst_short_loss_pct": round(worst_loss * 100, 2),
        # TRUE MTM (primary)
        "ann_sharpe": eq_stats_mtm["ann_sharpe"],
        "cagr": eq_stats_mtm["cagr"],
        "max_dd": eq_stats_mtm["max_dd"],
        "max_dd_date": eq_stats_mtm["max_dd_date"],
        "calmar": eq_stats_mtm["calmar"],
        "total_return": eq_stats_mtm["total_return"],
        # Exit-dump cross-check
        "ann_sharpe_exit_dump": eq_stats_exit["ann_sharpe"],
    }


# =====================================================================================
# REGIME BREAKDOWN
# =====================================================================================

def regime_breakdown(tdf: pd.DataFrame, label: str) -> None:
    if tdf.empty or "regime" not in tdf.columns:
        return
    print(f"\n  Regime breakdown [{label}]:")
    print(f"  {'regime':<12} {'N':>5} {'WR%':>7} {'avgRet%':>9} {'totalPnL':>10}")
    for regime in ["bullish", "ranging", "bearish", "unknown"]:
        grp = tdf[tdf["regime"] == regime]
        if grp.empty:
            continue
        pnls = grp["pnl_pct"].values
        n = len(pnls)
        wr = float((pnls > 0).mean()) * 100
        avg = float(pnls.mean()) * 100
        total = float(pnls.sum()) * POSITION_SIZE
        print(f"  {regime:<12} {n:>5} {wr:>6.1f}% {avg:>+8.2f}% {total:>+10,.0f}¥")


# =====================================================================================
# REVERSAL BASELINE
# =====================================================================================

def run_reversal_baseline() -> dict:
    print("[baseline] BNF reversal engine (long, cost-OFF)...")
    thresholds = compute_sector_thresholds_from_cache(k=2.0)
    res = run_backtest(
        sector_thresholds=thresholds,
        start_date=START,
        end_date=END,
        max_concurrent_positions=MAX_CONCURRENT,
        position_size=POSITION_SIZE,
        initial_balance=PORT_SIZE,
    )
    m = res.metrics()
    eq = res.equity_curve
    ann_sharpe = 0.0
    cagr = 0.0
    max_dd = 0.0
    max_dd_date = "N/A"
    calmar = float("nan")
    n = m.get("total_trades", 0)
    wins = m.get("wins", 0)
    if len(eq) > 1:
        daily_ret = eq.pct_change().dropna()
        if daily_ret.std() > 0:
            ann_sharpe = float((daily_ret.mean() / daily_ret.std()) * math.sqrt(252))
        total_ret = float(eq.iloc[-1] / eq.iloc[0] - 1.0)
        cagr = float((1.0 + total_ret) ** (1.0 / YEARS) - 1.0)
        peak = eq.cummax()
        dd = (eq - peak) / peak
        max_dd = float(dd.min())
        max_dd_idx = dd.idxmin()
        max_dd_date = str(max_dd_idx.date()) if hasattr(max_dd_idx, "date") else str(max_dd_idx)
        if max_dd < 0:
            calmar = cagr / abs(max_dd)

    wr = wins / n if n > 0 else 0.0
    wr_lo, wr_hi = _wilson_ci(wins, n)

    return {
        "label": "long_reversal_baseline",
        "cost_on": False,
        "n_trades": n,
        "win_rate": round(wr, 4),
        "wr_ci_lo": round(wr_lo, 4),
        "wr_ci_hi": round(wr_hi, 4),
        "avg_return": round(m.get("avg_return", 0.0), 5),
        "avg_ret_ci_lo": 0.0,
        "avg_ret_ci_hi": 0.0,
        "median_return": round(m.get("median_return", 0.0), 5),
        "avg_hold_days": 0.0,
        "worst_short_loss_pct": 0.0,
        "ann_sharpe": round(ann_sharpe, 3),
        "cagr": round(cagr, 4),
        "max_dd": round(max_dd, 4),
        "max_dd_date": max_dd_date,
        "calmar": round(calmar, 3),
        "total_return": round(float(eq.iloc[-1] / eq.iloc[0] - 1.0) if len(eq) > 1 else 0.0, 4),
        "ann_sharpe_exit_dump": round(ann_sharpe, 3),
    }


# =====================================================================================
# PRINT HELPERS
# =====================================================================================

def print_row(s: dict) -> None:
    if not s or s.get("n_trades", 0) == 0:
        print(f"  {s.get('label','?')}: NO TRADES")
        return
    cost_tag = " [COST-ON]" if s.get("cost_on") else ""
    xcheck = f" [MTM={s['ann_sharpe']:+.3f} exit-dump={s.get('ann_sharpe_exit_dump',0):+.3f}]"
    print(
        f"  {s['label']+cost_tag:<40}"
        f"  N={s['n_trades']:5d}"
        f"  WR={s['win_rate']*100:5.1f}%[{s['wr_ci_lo']*100:.1f},{s['wr_ci_hi']*100:.1f}]"
        f"  avgRet={s['avg_return']*100:+.2f}%"
        f"  CAGR={s['cagr']*100:+.1f}%"
        f"  annSharpe={s['ann_sharpe']:+.3f}"
        f"  maxDD={s['max_dd']*100:.1f}%({s['max_dd_date']})"
        f"  calmar={s['calmar']:.2f}"
        f"  hold={s['avg_hold_days']:.0f}d"
        f"{xcheck}"
    )


# =====================================================================================
# MAIN
# =====================================================================================

def main() -> None:
    print("=" * 130)
    print("3-6 空売り対称化 S1ゲート — short-side mirror of BNF reversal")
    print(f"  period={START}→{END}  ({YEARS:.2f}yr)  max_concurrent={MAX_CONCURRENT}  pos_size={POSITION_SIZE:,.0f}")
    print(f"  exit: stop_short(+{STOP_LOSS_SHORT*100:.0f}% rise) / mean_revert(dev<=0) / max_hold({MAX_HOLD_DAYS}d)")
    print(f"  P1'=dev>=+th, P2'=RSI>{RSI_OB_THRESHOLD}, P3'=close>BB_upper, P4'=nk_dev>0, P5'=vol_dec; consensus>=3→SHORT")
    print("=" * 130)

    results: list[dict] = []

    # ---- Spec A: All regimes (P4' not required) ----
    print(f"\n[1/3] Spec A: All regimes (P4' not required) — test raw overbought-reversion signal...")
    tdf_a = simulate_short_mirror(require_p4_bullish=False, cost_on=False)
    print(f"  → {len(tdf_a)} trades")
    if not tdf_a.empty:
        print(f"     Exit reasons: {tdf_a['regime'].value_counts().to_dict()}")
        print(f"     Worst single short loss: {tdf_a['worst_short_loss'].max()*100:.1f}%")
    s_a = compute_stats(tdf_a, "short_all_regimes", cost_on=False)
    print_row(s_a)
    regime_breakdown(tdf_a, "short_all_regimes")
    results.append(s_a)

    # ---- Spec A cost-ON ----
    print(f"\n[1b] Spec A COST-ON: {SLIPPAGE_BPS}bps round-trip slippage...")
    tdf_a_cost = simulate_short_mirror(require_p4_bullish=False, cost_on=True)
    s_a_cost = compute_stats(tdf_a_cost, "short_all_regimes", cost_on=True)
    print_row(s_a_cost)
    results.append(s_a_cost)

    # ---- Spec B: P4' = bullish market (nk_dev > 0) ----
    print(f"\n[2/3] Spec B: Bullish market gate (P4'=nk_dev>0 required)...")
    tdf_b = simulate_short_mirror(require_p4_bullish=True, cost_on=False)
    print(f"  → {len(tdf_b)} trades")
    if not tdf_b.empty:
        print(f"     Regime mix: {tdf_b['regime'].value_counts().to_dict()}")
    s_b = compute_stats(tdf_b, "short_bullish_gate", cost_on=False)
    print_row(s_b)
    regime_breakdown(tdf_b, "short_bullish_gate")
    results.append(s_b)

    # ---- Spec B cost-ON ----
    print(f"\n[2b] Spec B COST-ON...")
    tdf_b_cost = simulate_short_mirror(require_p4_bullish=True, cost_on=True)
    s_b_cost = compute_stats(tdf_b_cost, "short_bullish_gate", cost_on=True)
    print_row(s_b_cost)
    results.append(s_b_cost)

    # ---- Long reversal baseline ----
    print(f"\n[3/3] Long reversal baseline (reference)...")
    s_rev = run_reversal_baseline()
    print_row(s_rev)
    results.append(s_rev)

    # =====================================================================================
    # SCORECARD
    # =====================================================================================
    print()
    print("=" * 130)
    print("SCORECARD — 3-6 空売り対称化 vs 長側逆張り baseline")
    header = f"{'spec':<42} {'N':>5} {'WR%[lo,hi]':>20} {'avgRet%':>9} {'CAGR':>7} {'annSharpeMTM':>13} {'maxDD%':>8} {'maxDD_dt':>12} {'calmar':>7} {'hold':>5}"
    print(header)
    print("-" * 130)
    for s in results:
        if not s:
            continue
        wr_str = f"{s['win_rate']*100:.1f}%[{s['wr_ci_lo']*100:.1f},{s['wr_ci_hi']*100:.1f}]"
        cost_tag = "*" if s.get("cost_on") else " "
        lbl = cost_tag + s["label"]
        n = s.get("n_trades", 0)
        if n == 0:
            print(f"{lbl:<42}  {'NO TRADES':>5}")
            continue
        print(
            f"{lbl:<42} {n:>5} {wr_str:>20}"
            f" {s['avg_return']*100:>+8.2f}%"
            f" {s['cagr']*100:>+6.1f}%"
            f" {s['ann_sharpe']:>+13.3f}"
            f" {s['max_dd']*100:>7.1f}%"
            f" {s['max_dd_date']:>12}"
            f" {s['calmar']:>7.2f}"
            f" {s.get('avg_hold_days',0):>4.0f}d"
        )

    # =====================================================================================
    # VERDICT
    # =====================================================================================
    print()
    print("=" * 130)

    # Select best spec (highest cost-ON ann_sharpe)
    cost_on_specs = [s for s in results if s.get("cost_on") and s.get("n_trades", 0) > 0]
    if cost_on_specs:
        best_cost = max(cost_on_specs, key=lambda s: s.get("ann_sharpe", -999))
    else:
        best_cost = {"ann_sharpe": 0.0, "label": "N/A", "n_trades": 0}

    # Cost-OFF specs
    cost_off_specs = [s for s in results if not s.get("cost_on") and "short" in s.get("label","") and s.get("n_trades",0)>0]
    best_off = max(cost_off_specs, key=lambda s: s.get("ann_sharpe", -999)) if cost_off_specs else {"ann_sharpe": 0.0}

    rev_sharpe = s_rev.get("ann_sharpe", 0.0)
    best_cost_sharpe = best_cost.get("ann_sharpe", 0.0)
    best_off_sharpe = best_off.get("ann_sharpe", 0.0)
    avg_ret_ci_lo = best_off.get("avg_ret_ci_lo", 0.0) if cost_off_specs else 0.0

    # Verdict criteria for SHORT strategy (multi-gate — ALL must pass for GREEN/YELLOW):
    # GREEN:  cost-ON Sharpe > 0.5 AND avg return CI_lo > 0 AND CAGR > 0 AND n_trades > 50
    # YELLOW: cost-ON Sharpe > 0.2 AND CAGR > 0 AND n_trades > 30
    # RED:    any of: CAGR <= 0, avg_return < 0, maxDD = -100%, CI_lo < 0

    n_best = best_cost.get("n_trades", 0)
    best_cost_cagr = best_cost.get("cagr", -1.0)
    best_cost_maxdd = best_cost.get("max_dd", -1.0)
    best_off_avg = best_off.get("avg_return", -1.0) if cost_off_specs else -1.0
    avg_ret_positive = avg_ret_ci_lo > 0.0  # CI lower bound above zero
    cagr_positive = best_cost_cagr > 0.0
    catastrophic_dd = best_cost_maxdd <= -0.99  # near-total wipeout

    if best_cost_sharpe > 0.50 and avg_ret_positive and cagr_positive and n_best >= 50 and not catastrophic_dd:
        verdict = "GREEN"
        detail = "Real short-side edge after cost — annSharpe>0.5, avg return CI>0, CAGR>0"
    elif best_cost_sharpe > 0.20 and cagr_positive and n_best >= 30 and not catastrophic_dd:
        verdict = "YELLOW"
        detail = "Marginal/regime-conditional edge — cost-ON Sharpe>0.2 AND CAGR>0"
    else:
        if catastrophic_dd:
            detail = f"No edge — catastrophic maxDD={best_cost_maxdd*100:.0f}%, avg per-trade return is NEGATIVE ({best_off_avg*100:+.2f}%); short side loses money"
        elif not cagr_positive:
            detail = f"No edge — CAGR={best_cost_cagr*100:+.1f}% negative; short side loses money over full period"
        else:
            detail = "No clear short-side edge after cost — insufficient Sharpe or low trade count"
        verdict = "RED"

    print(f"VERDICT: {verdict} — {detail}")
    print(f"  Best cost-ON spec: {best_cost.get('label','N/A')}")
    print(f"  TRUE-MTM annSharpe (cost-OFF): {best_off_sharpe:+.3f}  (cost-ON): {best_cost_sharpe:+.3f}")
    print(f"  Long reversal baseline annSharpe: {rev_sharpe:+.3f}")
    print(f"  Short mirror as fraction of long: {best_off_sharpe/rev_sharpe:.2f}x" if rev_sharpe != 0 else "")
    print(f"  Cost drag: {best_off_sharpe - best_cost_sharpe:.3f}")
    print(f"  avg_return CI_lo: {avg_ret_ci_lo*100:+.3f}%  ({'CI>0' if avg_ret_ci_lo>0 else 'CI includes 0'})")
    if not tdf_a.empty:
        print(f"  Worst single-trade short loss (max adverse excursion): {tdf_a['worst_short_loss'].max()*100:.1f}%")
    print()
    print("BIGGEST CAVEAT (single):")
    print("  BORROW REALISM + SURVIVORSHIP BIAS: This backtest assumes all stocks are freely")
    print("  shortable (no hard-to-borrow, no borrow cost). In Japanese markets, overbought")
    print("  stocks (high deviation, high RSI) are OFTEN already on the securities lending")
    print("  shortage list — the very stocks this signal targets may be hard/impossible to")
    print("  borrow. Additionally, survivorship bias in the universe UNDERSTATES the short edge")
    print("  (delisted/bankrupt stocks, the best shorts, are missing). Together: reported edge")
    print("  is likely an UPPER BOUND on realizable PnL, not achievable as-is.")
    print()
    print("  Secondary: 2021-2026 was a structurally bullish period for Japan (Nikkei at ATH).")
    print("  Short strategies are theoretically favored in bearish/ranging — but shorts in a")
    print("  persistent bull market face unbounded tail risk (short squeeze). The 2024-08 crash")
    print("  was the primary window where shorts would have won.")
    print()
    print("SCRIPT: /Users/ryu/grove-stock/docs/grove-stock/research/3-6_short_mirror_gate.py")
    print("=" * 130)


if __name__ == "__main__":
    main()
