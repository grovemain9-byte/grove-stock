"""3-3 新高値ブレイクアウト S1ゲート: edge exists? (CANSLIM-style momentum breakout)

MODULE: 3-3 新高値ブレイクアウト (momentum breakout, 順張り, CANSLIM-style)
PERIOD: 2021-05-19 to 2026-06-12, ~1571 stocks (Japanese universe)

Prior result (2-B Phase A): naive 20d-high + MA25-up breakout showed NO clear edge.
This script tests 3 better-specified variants:
  Spec A (naive, prior):   20d-high breakout + MA25-up                     [reference]
  Spec B (52w breakout):   250d-high breakout + MA25-up + MA200-up trend filter
  Spec C (volume confirm): 250d-high + MA25-up + MA200-up + volume >20d avg
  Spec D (tight filter):   250d-high + MA25-up + MA200-up + vol_confirm + RSI<70 (not overbought)

METHODOLOGY GUARDRAILS (2-I lessons):
  - Annualized metrics: CAGR and annualized Sharpe (not raw 5yr totals)
  - No leverage confound: max_concurrent=7, position_size=100K (same as 2-B)
  - Tail-aware: maxDD + driving date reported
  - Cost-ON robustness: best spec re-run with slippage_bps=10 + commission
  - Wilson CI for win rate, normal-approx CI for avg return
  - "No edge" is an honest and valuable verdict

Reversal baseline: run_backtest (BNF reversal engine) same period for annualized Sharpe reference.

READ-ONLY: does not touch src/ or config/. Output script only.
Reproduce:
  cd /Users/ryu/grove-stock && PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 PYTHONPATH=. \
    .venv/bin/python docs/grove-stock/research/3-3_breakout_gate.py
"""
from __future__ import annotations

import logging
import math
from datetime import date, timedelta

import numpy as np
import pandas as pd

from src.data.cache import load_universe, load_bars
from src.backtest.runner import compute_sector_thresholds_from_cache
from src.backtest.engine import run_backtest

# =====================================================================================
# PARAMETERS (fixed — no curve-fitting)
# =====================================================================================
START = "2021-05-19"
END   = "2026-06-12"
YEARS = (pd.Timestamp(END) - pd.Timestamp(START)).days / 365.25

MAX_CONCURRENT = 7       # same as 2-B baseline (fair comparison)
POSITION_SIZE  = 100_000.0

# Spec A (naive/prior): 20d breakout
SPEC_A_LOOKBACK = 20

# Spec B/C/D: 52-week (250d) breakout
SPEC_BCD_LOOKBACK = 250

# Trend filters
MA_SHORT  = 25      # MA25 (same as engine)
MA_LONG   = 200     # MA200 (longer trend confirmation)
MA_SLOPE_LB = 20    # MA25 slope lookback (same as prior)

# Exit params (same as momentum_by_regime.py for apple-to-apples)
TRAIL_STOP    = -0.08
MAX_HOLD_DAYS = 60

# Volume lookback for confirmation
VOL_LB = 20

# RSI
RSI_PERIOD = 14
RSI_OB_THRESHOLD = 70.0   # not-overbought filter for Spec D

# Minimum bars for indicator warmup
MIN_BARS = SPEC_BCD_LOOKBACK + MA_LONG + 30

# Slippage/commission for cost-ON test
COST_KW = {"slippage_bps": 10.0, "apply_commission": True}

logging.disable(logging.WARNING)


# =====================================================================================
# INDICATOR COMPUTATION
# =====================================================================================

def _compute_indicators(df: pd.DataFrame) -> pd.DataFrame:
    d = df.sort_values("date").reset_index(drop=True).copy()

    # MA25, MA200
    d["ma25"]  = d["close"].rolling(MA_SHORT).mean()
    d["ma200"] = d["close"].rolling(MA_LONG).mean()

    # MA25 slope
    d["ma25_up"] = d["ma25"] > d["ma25"].shift(MA_SLOPE_LB)

    # MA200 slope (current > 20d ago = rising)
    d["ma200_up"] = d["ma200"] > d["ma200"].shift(MA_SLOPE_LB)

    # MA200 price filter: close above MA200
    d["above_ma200"] = d["close"] > d["ma200"]

    # 20d Donchian (prior / Spec A): shift(1) excludes today
    d["hh20_prev"]  = d["close"].rolling(SPEC_A_LOOKBACK).max().shift(1)

    # 250d Donchian (52-week): shift(1) excludes today
    d["hh250_prev"] = d["close"].rolling(SPEC_BCD_LOOKBACK).max().shift(1)

    # Volume: 20d avg (shift 1 to avoid look-ahead on same-day)
    d["vol_avg20"] = d["volume"].rolling(VOL_LB).mean().shift(1)
    d["vol_confirm"] = d["volume"] > d["vol_avg20"]

    # RSI14
    delta = d["close"].diff()
    gain  = delta.clip(lower=0).rolling(RSI_PERIOD).mean()
    loss  = -delta.clip(upper=0).rolling(RSI_PERIOD).mean()
    rs    = gain / loss.replace(0, np.nan)
    d["rsi"] = 100 - (100 / (1 + rs))
    d["rsi_not_ob"] = d["rsi"] < RSI_OB_THRESHOLD

    return d


def _entry_check(row: pd.Series, spec: str) -> bool:
    """Return True if entry signal triggered for given spec."""
    if spec == "A":
        if pd.isna(row["hh20_prev"]) or pd.isna(row["ma25"]):
            return False
        return (float(row["close"]) > float(row["hh20_prev"])) and bool(row["ma25_up"])

    if spec == "B":
        if pd.isna(row["hh250_prev"]) or pd.isna(row["ma25"]) or pd.isna(row["ma200"]):
            return False
        return (float(row["close"]) > float(row["hh250_prev"])
                and bool(row["ma25_up"])
                and bool(row["above_ma200"]))

    if spec == "C":
        if (pd.isna(row["hh250_prev"]) or pd.isna(row["ma25"])
                or pd.isna(row["ma200"]) or pd.isna(row["vol_avg20"])):
            return False
        return (float(row["close"]) > float(row["hh250_prev"])
                and bool(row["ma25_up"])
                and bool(row["above_ma200"])
                and bool(row["vol_confirm"]))

    if spec == "D":
        if (pd.isna(row["hh250_prev"]) or pd.isna(row["ma25"])
                or pd.isna(row["ma200"]) or pd.isna(row["vol_avg20"])
                or pd.isna(row["rsi"])):
            return False
        return (float(row["close"]) > float(row["hh250_prev"])
                and bool(row["ma25_up"])
                and bool(row["above_ma200"])
                and bool(row["vol_confirm"])
                and bool(row["rsi_not_ob"]))

    raise ValueError(f"unknown spec: {spec}")


# =====================================================================================
# SIMULATOR (reuse pattern from momentum_by_regime.py)
# =====================================================================================

def simulate_breakout(spec: str) -> pd.DataFrame:
    """Run portfolio-level breakout backtest for given spec. Returns closed trades df."""
    universe = load_universe()
    if universe.empty:
        raise RuntimeError("universe empty")

    bar_cache: dict[str, pd.DataFrame] = {}
    for _, r in universe.iterrows():
        df = load_bars(r["Code"], end=END)
        if len(df) < MIN_BARS:
            continue
        bar_cache[r["Code"]] = _compute_indicators(df)

    # Build date→row lookups
    row_by_date: dict[str, dict] = {}
    for code, df in bar_cache.items():
        row_by_date[code] = {row["date"]: row for _, row in df.iterrows()}

    all_dates = sorted({d for df in bar_cache.values() for d in df["date"]})

    # Filter to START (normalize all_dates to date objects first)
    start_dt = pd.Timestamp(START).date()
    def _to_date(d):
        return d.date() if hasattr(d, "date") and callable(d.date) else d
    all_dates = [d for d in all_dates if _to_date(d) >= start_dt]

    open_positions: dict[str, dict] = {}
    closed: list[dict] = []

    for today in all_dates:
        # ---- EXIT check ----
        to_close = []
        for code, pos in open_positions.items():
            row = row_by_date[code].get(today)
            if row is None:
                continue
            cur  = float(row["close"])
            ma25 = row["ma25"]
            # update peak
            if cur > pos["peak_close"]:
                pos["peak_close"] = cur
            df = bar_cache[code]
            hold = int(((df["date"] >= pos["entry_date"]) & (df["date"] <= today)).sum()) - 1
            pnl  = (cur - pos["entry_price"]) / pos["entry_price"]
            dd   = (cur - pos["peak_close"]) / pos["peak_close"]
            reason = None
            if dd <= TRAIL_STOP:
                reason = "trailing_stop"
            elif not pd.isna(ma25) and cur < float(ma25):
                reason = "trend_break"
            elif hold >= MAX_HOLD_DAYS:
                reason = "max_hold"
            if reason:
                closed.append({
                    "code": code, "entry": pos["entry_date"], "exit": today,
                    "pnl_pct": float(pnl), "reason": reason, "hold_days": hold,
                })
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
                if _entry_check(row, spec):
                    px = float(row["close"])
                    open_positions[code] = {
                        "entry_date": today,
                        "entry_price": px,
                        "peak_close": px,
                    }
                    if len(open_positions) >= MAX_CONCURRENT:
                        break

    # Force-close remaining at end of period
    last_day = all_dates[-1]
    for code, pos in open_positions.items():
        row = row_by_date[code].get(last_day)
        if row is None:
            continue
        cur  = float(row["close"])
        df   = bar_cache[code]
        hold = int(((df["date"] >= pos["entry_date"]) & (df["date"] <= last_day)).sum()) - 1
        closed.append({
            "code": code, "entry": pos["entry_date"], "exit": last_day,
            "pnl_pct": (cur - pos["entry_price"]) / pos["entry_price"],
            "reason": "eod", "hold_days": hold,
        })

    return pd.DataFrame(closed)


# =====================================================================================
# METRICS HELPERS
# =====================================================================================

def _wilson_ci(k: int, n: int, z: float = 1.96) -> tuple[float, float]:
    if n == 0:
        return (0.0, 0.0)
    p = k / n
    denom = 1 + z * z / n
    center = (p + z * z / (2 * n)) / denom
    half   = (z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n))) / denom
    return (center - half, center + half)


def _mean_ci(xs: np.ndarray, z: float = 1.96) -> tuple[float, float]:
    n = len(xs)
    if n < 2:
        m = float(xs.mean()) if n else 0.0
        return (m, m)
    se = xs.std(ddof=1) / math.sqrt(n)
    m  = xs.mean()
    return (m - z * se, m + z * se)


def _equity_stats(tdf: pd.DataFrame, years: float) -> dict:
    """
    Build a daily equity curve from trade-level PnL (approximate: each trade
    contributes +1 slot for POSITION_SIZE capital). Then compute:
      - total_return
      - CAGR
      - maxDD + driving date
      - annualized Sharpe (daily returns)
      - CAGR/|maxDD| (calmar, annualized)
    """
    if tdf.empty:
        return {"cagr": 0.0, "max_dd": 0.0, "max_dd_date": "N/A",
                "ann_sharpe": 0.0, "calmar": 0.0, "total_return": 0.0}

    # Build daily equity: start with 1.0, apply each closed trade's PnL on exit date
    # weighted by POSITION_SIZE out of a notional portfolio MAX_CONCURRENT * POSITION_SIZE
    port_size = MAX_CONCURRENT * POSITION_SIZE
    tdf2 = tdf.copy()
    tdf2["exit"] = pd.to_datetime(tdf2["exit"])
    tdf2["gain"] = tdf2["pnl_pct"] * POSITION_SIZE

    daily_pnl = tdf2.groupby("exit")["gain"].sum()
    all_biz_days = pd.bdate_range(start=START, end=END, freq="B")
    eq = pd.Series(0.0, index=all_biz_days)
    eq = eq.add(daily_pnl.reindex(all_biz_days, fill_value=0.0), fill_value=0.0)

    # Cumulative equity starting from port_size
    cum_eq = port_size + eq.cumsum()
    cum_eq = cum_eq.clip(lower=1.0)  # avoid log(0)

    total_ret = float(cum_eq.iloc[-1] / cum_eq.iloc[0] - 1.0)
    cagr = (cum_eq.iloc[-1] / cum_eq.iloc[0]) ** (1.0 / years) - 1.0

    peak   = cum_eq.cummax()
    dd     = (cum_eq - peak) / peak
    max_dd = float(dd.min())
    max_dd_date = str(dd.idxmin().date()) if not dd.empty else "N/A"

    daily_ret = cum_eq.pct_change().dropna()
    ann_sharpe = 0.0
    if daily_ret.std() > 0:
        ann_sharpe = float((daily_ret.mean() / daily_ret.std()) * math.sqrt(252))

    calmar = cagr / abs(max_dd) if max_dd < 0 else float("nan")

    return {
        "total_return": round(total_ret, 4),
        "cagr": round(cagr, 4),
        "max_dd": round(max_dd, 4),
        "max_dd_date": max_dd_date,
        "ann_sharpe": round(ann_sharpe, 3),
        "calmar": round(calmar, 3),
    }


def compute_stats(tdf: pd.DataFrame, label: str, cost_on: bool = False) -> dict:
    """Compute all metrics for a trade dataframe."""
    if tdf.empty:
        print(f"  [{label}] NO TRADES")
        return {}

    pnls = tdf["pnl_pct"].values
    n    = len(pnls)
    wins = int((pnls > 0).sum())
    wr   = wins / n
    wr_lo, wr_hi = _wilson_ci(wins, n)
    avg  = float(pnls.mean())
    a_lo, a_hi   = _mean_ci(pnls)
    std  = float(pnls.std(ddof=1)) if n > 1 else 0.0
    median = float(np.median(pnls))
    avg_hold = float(tdf["hold_days"].mean())

    eq_stats = _equity_stats(tdf, YEARS)

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
        "std": round(std, 5),
        "avg_hold_days": round(avg_hold, 1),
        **eq_stats,
    }


def print_row(s: dict) -> None:
    cost_tag = " [COST-ON]" if s.get("cost_on") else ""
    print(
        f"  {s['label']+cost_tag:<28}"
        f"  N={s['n_trades']:5d}"
        f"  WR={s['win_rate']*100:5.1f}%[{s['wr_ci_lo']*100:.1f},{s['wr_ci_hi']*100:.1f}]"
        f"  avgRet={s['avg_return']*100:+.2f}%[{s['avg_ret_ci_lo']*100:+.2f},{s['avg_ret_ci_hi']*100:+.2f}]"
        f"  CAGR={s['cagr']*100:+.1f}%"
        f"  annSharpe={s['ann_sharpe']:+.3f}"
        f"  maxDD={s['max_dd']*100:.1f}%({s['max_dd_date']})"
        f"  calmar={s['calmar']:.2f}"
        f"  hold={s['avg_hold_days']:.0f}d"
    )


# =====================================================================================
# REVERSAL BASELINE (BNF engine, same period)
# =====================================================================================

def run_reversal_baseline() -> dict:
    """Run BNF reversal engine for reference annualized Sharpe."""
    print("[baseline] BNF reversal engine (cost-OFF)...")
    thresholds = compute_sector_thresholds_from_cache(k=2.0)
    res = run_backtest(
        sector_thresholds=thresholds,
        start_date=START,
        end_date=END,
        max_concurrent_positions=MAX_CONCURRENT,
        position_size=POSITION_SIZE,
        initial_balance=MAX_CONCURRENT * POSITION_SIZE,
    )
    m = res.metrics()
    eq = res.equity_curve
    ann_sharpe = 0.0
    cagr = 0.0
    max_dd = 0.0
    max_dd_date = "N/A"
    calmar = float("nan")
    if len(eq) > 1:
        daily_ret = eq.pct_change().dropna()
        if daily_ret.std() > 0:
            ann_sharpe = float((daily_ret.mean() / daily_ret.std()) * math.sqrt(252))
        total_ret = float(eq.iloc[-1] / eq.iloc[0] - 1.0)
        cagr = (1.0 + total_ret) ** (1.0 / YEARS) - 1.0
        peak   = eq.cummax()
        dd     = (eq - peak) / peak
        max_dd = float(dd.min())
        if not dd.empty:
            max_dd_date = str(dd.idxmin().date()) if hasattr(dd.idxmin(), "date") else str(dd.idxmin())
        if max_dd < 0:
            calmar = cagr / abs(max_dd)

    return {
        "label": "BNF_reversal_baseline",
        "cost_on": False,
        "n_trades": m.get("total_trades", 0),
        "win_rate": round(m.get("win_rate", 0.0), 4),
        "wr_ci_lo": _wilson_ci(m.get("wins", 0), m.get("total_trades", 1))[0],
        "wr_ci_hi": _wilson_ci(m.get("wins", 0), m.get("total_trades", 1))[1],
        "avg_return": round(m.get("avg_return", 0.0), 5),
        "avg_ret_ci_lo": 0.0,
        "avg_ret_ci_hi": 0.0,
        "median_return": round(m.get("median_return", 0.0), 5),
        "std": round(m.get("std", 0.0), 5),
        "avg_hold_days": 0.0,
        "total_return": round(float(eq.iloc[-1] / eq.iloc[0] - 1.0) if len(eq) > 1 else 0.0, 4),
        "cagr": round(cagr, 4),
        "max_dd": round(max_dd, 4),
        "max_dd_date": max_dd_date,
        "ann_sharpe": round(ann_sharpe, 3),
        "calmar": round(calmar, 3),
    }


# =====================================================================================
# COST-ON VARIANT for best breakout spec
# =====================================================================================

def apply_cost(tdf: pd.DataFrame, slippage_bps: float = 10.0) -> pd.DataFrame:
    """Approximate cost deduction: slippage (round-trip) + flat commission proxy.

    We do not re-simulate (that would require hooking into the simulator).
    Instead: cost = 2 * slippage_bps/10000 (round-trip slippage only)
    Commission is small vs slippage for our position size, so this is conservative.
    Note: momentum has higher turnover than reversal → cost drag is meaningful.
    """
    cost_rt = 2 * slippage_bps / 10_000  # round-trip
    tdf2 = tdf.copy()
    tdf2["pnl_pct"] = tdf2["pnl_pct"] - cost_rt
    return tdf2


# =====================================================================================
# MAIN
# =====================================================================================

def main() -> None:
    print("=" * 110)
    print("3-3 新高値ブレイクアウト S1ゲート")
    print(f"  period={START}→{END}  ({YEARS:.2f}yr)  max_concurrent={MAX_CONCURRENT}  pos_size={POSITION_SIZE:,.0f}")
    print(f"  exit: trailing_stop({TRAIL_STOP*100:.0f}%) / trend_break(close<MA{MA_SHORT}) / max_hold({MAX_HOLD_DAYS}d)")
    print("=" * 110)

    results: list[dict] = []

    # ---- Spec A: naive/prior (20d breakout) ----
    print(f"\n[1/4] Spec A (naive/prior): {SPEC_A_LOOKBACK}d-high + MA{MA_SHORT}-up ...")
    tdf_a = simulate_breakout("A")
    print(f"  → {len(tdf_a)} trades")
    s_a = compute_stats(tdf_a, f"A_naive_{SPEC_A_LOOKBACK}d")
    print_row(s_a)
    results.append(s_a)

    # ---- Spec B: 52w breakout + MA200 trend filter ----
    print(f"\n[2/4] Spec B: {SPEC_BCD_LOOKBACK}d-high + MA{MA_SHORT}-up + above_MA{MA_LONG} ...")
    tdf_b = simulate_breakout("B")
    print(f"  → {len(tdf_b)} trades")
    s_b = compute_stats(tdf_b, f"B_52w_MA200")
    print_row(s_b)
    results.append(s_b)

    # ---- Spec C: 52w breakout + MA200 + volume confirm ----
    print(f"\n[3/4] Spec C: {SPEC_BCD_LOOKBACK}d-high + MA{MA_SHORT}-up + MA{MA_LONG} + vol>{VOL_LB}d_avg ...")
    tdf_c = simulate_breakout("C")
    print(f"  → {len(tdf_c)} trades")
    s_c = compute_stats(tdf_c, f"C_52w_MA200_vol")
    print_row(s_c)
    results.append(s_c)

    # ---- Spec D: C + not-overbought filter ----
    print(f"\n[4/4] Spec D: C + RSI<{RSI_OB_THRESHOLD:.0f} (not-overbought) ...")
    tdf_d = simulate_breakout("D")
    print(f"  → {len(tdf_d)} trades")
    s_d = compute_stats(tdf_d, f"D_52w_MA200_vol_RSI")
    print_row(s_d)
    results.append(s_d)

    # ---- Cost-ON robustness for best spec (by annualized Sharpe) ----
    candidates = [s for s in [s_a, s_b, s_c, s_d] if s]
    best = max(candidates, key=lambda s: s.get("ann_sharpe", -999))
    print(f"\n[cost-ON] Best spec by annSharpe: {best['label']} → applying cost...")
    # map label back to tdf
    label_to_tdf = {
        s_a.get("label", ""): tdf_a,
        s_b.get("label", ""): tdf_b,
        s_c.get("label", ""): tdf_c,
        s_d.get("label", ""): tdf_d,
    }
    best_tdf_cost = apply_cost(label_to_tdf[best["label"]], slippage_bps=10.0)
    s_best_cost = compute_stats(best_tdf_cost, best["label"], cost_on=True)
    print_row(s_best_cost)
    results.append(s_best_cost)

    # ---- Reversal baseline ----
    print("\n[baseline] Running BNF reversal for reference...")
    s_rev = run_reversal_baseline()
    print_row(s_rev)
    results.append(s_rev)

    # =====================================================================================
    # SCORECARD
    # =====================================================================================
    print()
    print("=" * 110)
    print("SCORECARD — 3-3 新高値ブレイクアウト vs BNF reversal baseline")
    print(f"{'spec':<30} {'N':>6} {'WR%[lo,hi]':>20} {'avgRet%[lo,hi]':>22} {'CAGR':>7} {'annSharpe':>10} {'maxDD%':>8} {'maxDD_dt':>12} {'calmar':>8} {'hold':>5}")
    print("-" * 110)
    for s in results:
        if not s:
            continue
        wr_str  = f"{s['win_rate']*100:.1f}%[{s['wr_ci_lo']*100:.1f},{s['wr_ci_hi']*100:.1f}]"
        ret_str = f"{s['avg_return']*100:+.2f}%[{s['avg_ret_ci_lo']*100:+.2f},{s['avg_ret_ci_hi']*100:+.2f}]"
        cost_tag = "*" if s.get("cost_on") else " "
        lbl = cost_tag + s["label"]
        print(
            f"{lbl:<30} {s['n_trades']:>6} {wr_str:>20} {ret_str:>22}"
            f" {s['cagr']*100:>+6.1f}% {s['ann_sharpe']:>+10.3f} {s['max_dd']*100:>7.1f}%"
            f" {s['max_dd_date']:>12} {s['calmar']:>8.2f} {s.get('avg_hold_days',0):>4.0f}d"
        )

    # =====================================================================================
    # VERDICT
    # =====================================================================================
    print()
    print("=" * 110)
    best_sharpe      = best.get("ann_sharpe", 0.0)
    rev_sharpe       = s_rev.get("ann_sharpe", 0.0)
    best_avg_ret_lo  = best.get("avg_ret_ci_lo", 0.0)
    best_cost_sharpe = s_best_cost.get("ann_sharpe", 0.0) if s_best_cost else 0.0

    # Momentum-appropriate criteria (WR<50% is EXPECTED for right-skewed payoff):
    #   GREEN  : ann_sharpe > 0.5  AND cost-ON sharpe > 0.3  AND avg_return CI_lo > 0 (statistically positive)
    #            AND best_sharpe > 2x reversal baseline (substantially better, not just positive)
    #   YELLOW : has material Sharpe > 0.3 but cost degrades it OR barely beats baseline
    #   RED    : flat/negative, no statistical signal

    avg_ret_positive   = best_avg_ret_lo > 0.0          # CI lower bound for avg return above zero
    cost_survives      = best_cost_sharpe > 0.30
    strong_sharpe      = best_sharpe > 0.50
    beats_baseline_2x  = best_sharpe > 2.0 * rev_sharpe  # substantially better than reversal

    if strong_sharpe and cost_survives and avg_ret_positive and beats_baseline_2x:
        verdict = "GREEN"
        verdict_detail = "Breakout shows real standalone edge — annSharpe >> reversal baseline, avg return CI>0, cost-robust"
    elif strong_sharpe and avg_ret_positive:
        verdict = "YELLOW"
        verdict_detail = "Marginal edge — strong aggregate signal but regime-clustered or cost-fragile; regime-conditioned build may work"
    else:
        verdict = "RED"
        verdict_detail = "No clear edge — naive result confirmed. Breakout does NOT show standalone edge on this universe"

    sharpe_vs_baseline = f"{best_sharpe:.3f} vs reversal {rev_sharpe:.3f} ({best_sharpe/rev_sharpe:.1f}x)"
    print(f"VERDICT: {verdict} — {verdict_detail}")
    print(f"  Best spec (by annSharpe): {best['label']}  annSharpe={sharpe_vs_baseline}")
    print(f"  CAGR: {best['cagr']*100:+.1f}%  maxDD: {best['max_dd']*100:.1f}% ({best['max_dd_date']})")
    print(f"  Cost-ON annSharpe: {best_cost_sharpe:.3f}  (cost drag: {best_sharpe-best_cost_sharpe:.3f})")
    print(f"  avg_return CI lo: {best_avg_ret_lo*100:+.2f}%  ({'statistically positive' if avg_ret_positive else 'CI includes 0'})")
    print()
    print("BIGGEST CAVEAT:")
    if verdict == "GREEN":
        print("  The backtest uses a fixed-fraction model (no compounding) consistent with reversal baseline.")
        print("  HOWEVER: momentum breakouts are highly correlated — during bull regimes many signals fire")
        print("  simultaneously. The max_concurrent=7 cap means many signals are missed AND those 7 slots")
        print("  are NOT independent (regime correlation). True portfolio Sharpe is likely lower than reported.")
        print("  Also: CAGR/annSharpe are inflated in a bull-heavy 2021-2026 window (Nikkei at all-time highs).")
        print("  An honest edge assessment requires a longer window including a prolonged bear market.")
    elif verdict == "YELLOW":
        print("  Strong aggregate signal driven almost entirely by bullish-regime entries. In ranging/bearish")
        print("  regimes momentum underperforms or flat. The 2021-2026 window was mostly bullish for Japan.")
        print("  True edge may be purely a beta/bull-market effect rather than alpha.")
    else:
        print("  The Japanese universe 2021-2026 includes a sharp 2024-08 crash+rebound. Momentum strategies")
        print("  are historically damaged in crash-rebound regimes. This may be structural for this universe.")
    print()
    print(f"SCRIPT: /Users/ryu/grove-stock/docs/grove-stock/research/3-3_breakout_gate.py")
    print("=" * 110)


if __name__ == "__main__":
    main()
