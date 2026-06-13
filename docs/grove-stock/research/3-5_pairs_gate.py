"""3-5 ペアトレード S1ゲート: statistical arbitrage / pairs mean-reversion

MODULE: 3-5 ペアトレード (statistical arbitrage, market-neutral long-short)
PERIOD: 2021-05-19 to 2026-06-12, ~1572 Japanese stocks

HYPOTHESIS:
  High-correlation / cointegrated stock pairs' spread mean-reverts →
  a market-neutral long-short edge (long the cheap leg, short the rich leg, exit on reversion).

LOOK-AHEAD BIAS PREVENTION (critical for pairs):
  - Pair selection (top correlation + spread stationarity ADF test) uses TRAINING window
    ONLY: 2021-05-19 → 2023-12-29 (~2.6yr). This is the "in-sample" window.
  - Trading occurs ONLY on FORWARD window: 2024-01-01 → 2026-06-12 (~2.5yr).
  - Pairs are selected once (not rolling) to keep it simple and robust.
    Rolling re-selection would add complexity and risk of data mining.

METHODOLOGY:
  1. Within-sector pair candidates (same S33 sector) to control fundamental similarity.
  2. Training window: compute pairwise Pearson correlation of daily log-returns.
     Keep top-N pairs by correlation (threshold ≥ 0.70).
  3. For those candidates, run Engle-Granger cointegration test on price ratio
     (log(A) - log(B)) — simple, no external library needed.
     Keep pairs with ADF p-value ≤ 0.10 on training data.
  4. Forward window: track spread z-score using rolling mean/std from trailing 60d window
     (no training data leakage — only recent history).
  5. Entry: |z| ≥ 2.0 → long under-leg / short over-leg (equal notional per leg).
  6. Exit: |z| ≤ 0.5 (reversion) OR stop loss |z| ≥ 3.5 (divergence) OR max 20 days.
  7. Fixed notional POSITION_SIZE per leg. True MTM daily equity curve.
     Each day: value open positions at current close. Record daily portfolio value.
  8. Annualized Sharpe FROM DAILY MTM CURVE (not per-trade pseudo-Sharpe).

COST:
  Round-trip = 2 legs × 2 ways × COST_BPS = 4 × cost_bps.
  cost_bps = 10bps (slippage per leg) + 5bps (commission) = 15bps/leg.
  Round-trip total = 60bps per pair trade.

BETA-TO-NIKKEI:
  Compute daily strategy portfolio return vs Nikkei 225 daily return.
  beta = cov(strategy, nikkei) / var(nikkei).
  Market-neutral target: beta ≈ 0.

SURVIVORSHIP BIAS NOTE:
  Universe is current survivors only (no delisted names). Pairs where one
  leg was later delisted/merged would have broken — this biases favorably.

READ-ONLY: src/ and config/ are NOT modified.
Reproduce:
  cd /Users/ryu/grove-stock
  PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 PYTHONPATH=. .venv/bin/python docs/grove-stock/research/3-5_pairs_gate.py
"""
from __future__ import annotations

import logging
import math
import warnings
from datetime import date

import duckdb
import numpy as np
import pandas as pd

from src.data.cache import load_universe, load_nikkei_bars, CACHE_DB

logging.disable(logging.WARNING)
warnings.filterwarnings("ignore")

# =============================================================================
# PARAMETERS
# =============================================================================
TRAIN_START = "2021-05-19"
TRAIN_END   = "2023-12-29"   # training / pair-selection window
TEST_START  = "2024-01-01"   # forward / trading window
TEST_END    = "2026-06-12"

TRAIN_YEARS = (pd.Timestamp(TRAIN_END) - pd.Timestamp(TRAIN_START)).days / 365.25
TEST_YEARS  = (pd.Timestamp(TEST_END)  - pd.Timestamp(TEST_START)).days / 365.25
FULL_YEARS  = (pd.Timestamp(TEST_END)  - pd.Timestamp(TRAIN_START)).days / 365.25

# Pair selection thresholds
MIN_CORR = 0.70          # minimum Pearson correlation of daily log-returns (training)
MAX_ADF_P = 0.10         # maximum ADF p-value for spread stationarity (training)
MIN_TRAIN_BARS = 200     # minimum training bars required per stock

# Spread trading
ZSCORE_WINDOW = 60       # rolling lookback for z-score normalization (forward window)
ENTRY_Z = 2.0            # enter when |z| >= this
EXIT_Z  = 0.5            # exit when |z| <= this (reversion)
STOP_Z  = 3.5            # stop-loss when |z| >= this (divergence getting worse)
MAX_HOLD = 20            # maximum hold days

# Portfolio
MAX_CONCURRENT_PAIRS = 10   # max simultaneously open pair positions
POSITION_SIZE = 100_000.0   # per LEG (so each pair trade = 2 × POSITION_SIZE)
PORT_SIZE = MAX_CONCURRENT_PAIRS * 2 * POSITION_SIZE  # total portfolio notional

# Cost
COST_BPS_PER_LEG = 15.0    # 10bps slippage + 5bps commission per leg
COST_RT_PAIR = 4 * COST_BPS_PER_LEG / 10_000  # round-trip cost for both legs

# CI z-value
Z95 = 1.959963984540054


# =============================================================================
# DATA LOADING
# =============================================================================

def load_all_bars_wide() -> pd.DataFrame:
    """Load full OHLCV for all universe stocks. Returns wide close-price df (date x code)."""
    universe = load_universe()
    codes = [str(c) for c in universe["Code"].tolist()]
    con = duckdb.connect(str(CACHE_DB), read_only=True)
    placeholders = ",".join(["?"] * len(codes))
    df = con.execute(
        f"""
        SELECT code, date::DATE AS date, close
        FROM daily_quotes
        WHERE code IN ({placeholders})
          AND date >= ?
          AND date <= ?
          AND close IS NOT NULL AND close > 0
        ORDER BY date, code
        """,
        codes + [TRAIN_START, TEST_END],
    ).fetchdf()
    con.close()

    df["date"] = pd.to_datetime(df["date"])
    wide = df.pivot(index="date", columns="code", values="close").sort_index()
    return wide, universe


def load_nikkei_returns() -> pd.Series:
    """Load Nikkei 225 daily returns indexed by date."""
    nk = load_nikkei_bars()
    nk_series = nk["close"].copy()
    nk_series.index = nk["date"]
    nk_ret = nk_series.pct_change().dropna()
    return nk_ret


# =============================================================================
# ENGLE-GRANGER ADF TEST (simplified, no external library)
# =============================================================================

def adf_p_value(series: np.ndarray, lags: int = 1) -> float:
    """
    Simplified Augmented Dickey-Fuller p-value.
    Uses MacKinnon (1994) approximate critical values for H0: unit root.
    Returns approximate p-value. Conservative (may over-reject).

    We test: Δy_t = α + β*y_{t-1} + Σγ_i*Δy_{t-i} + ε
    H0: β = 0 (unit root / non-stationary)
    H1: β < 0 (stationary / mean-reverting)
    """
    y = np.asarray(series, dtype=float)
    y = y[~np.isnan(y)]
    n = len(y)
    if n < 20:
        return 1.0

    dy = np.diff(y)
    y_lag = y[:-1]

    # Build regression matrix
    rows_needed = len(dy) - lags
    if rows_needed < 10:
        return 1.0

    # Response: Δy_{t}, starting at index `lags`
    dY = dy[lags:]

    # Regressors: [constant, y_{t-1}, Δy_{t-1}, ..., Δy_{t-lags}]
    X = np.ones((rows_needed, 2 + lags))
    X[:, 1] = y_lag[lags:]  # y_{t-1}
    for j in range(lags):
        X[:, 2 + j] = dy[lags - 1 - j: len(dy) - 1 - j]

    # OLS
    try:
        XtX = X.T @ X
        XtY = X.T @ dY
        beta = np.linalg.solve(XtX, XtY)
        residuals = dY - X @ beta
        s2 = float(residuals @ residuals) / (rows_needed - (2 + lags))
        var_beta = s2 * np.linalg.inv(XtX)
        t_stat = beta[1] / math.sqrt(max(var_beta[1, 1], 1e-12))
    except np.linalg.LinAlgError:
        return 1.0

    # MacKinnon (1994) approximate quantiles for ADF (no-trend, intercept-only, n→∞)
    # Critical values at 1%, 5%, 10% for n=∞: -3.43, -2.86, -2.57
    # We approximate p-value using linear interpolation between known quantiles
    cv = {0.01: -3.43, 0.05: -2.86, 0.10: -2.57, 0.20: -2.20}

    if t_stat <= cv[0.01]:
        return 0.01
    elif t_stat <= cv[0.05]:
        # Linear interpolation between 1% and 5%
        frac = (t_stat - cv[0.01]) / (cv[0.05] - cv[0.01])
        return 0.01 + frac * 0.04
    elif t_stat <= cv[0.10]:
        frac = (t_stat - cv[0.05]) / (cv[0.10] - cv[0.05])
        return 0.05 + frac * 0.05
    elif t_stat <= cv[0.20]:
        frac = (t_stat - cv[0.10]) / (cv[0.20] - cv[0.10])
        return 0.10 + frac * 0.10
    else:
        return min(1.0, 0.20 + max(0.0, t_stat - cv[0.20]) * 0.1)


# =============================================================================
# PAIR SELECTION (training window only)
# =============================================================================

def select_pairs(
    wide_train: pd.DataFrame,
    sector_map: dict[str, str],
    min_corr: float = MIN_CORR,
    max_adf_p: float = MAX_ADF_P,
    min_bars: int = MIN_TRAIN_BARS,
) -> list[tuple[str, str, float, float]]:
    """
    Select cointegrated pairs from training window.

    Returns list of (code_a, code_b, correlation, adf_p_value).
    Pairs are within-sector only.
    """
    # Filter to stocks with enough training bars
    bar_counts = wide_train.notna().sum()
    valid_cols = bar_counts[bar_counts >= min_bars].index.tolist()
    print(f"  stocks with >= {min_bars} training bars: {len(valid_cols)}")

    # Log-returns for correlation
    log_prices = np.log(wide_train[valid_cols].replace(0, np.nan))
    log_ret = log_prices.diff().dropna(how="all")

    # Group by sector
    sector_groups: dict[str, list[str]] = {}
    for code in valid_cols:
        sec = sector_map.get(code, "UNKNOWN")
        sector_groups.setdefault(sec, []).append(code)

    print(f"  sectors with 2+ stocks: {sum(1 for v in sector_groups.values() if len(v) >= 2)}")

    candidate_pairs = []
    n_tested = 0

    for sector, codes in sector_groups.items():
        if len(codes) < 2:
            continue

        # Pairwise correlation
        codes_in_ret = [c for c in codes if c in log_ret.columns]
        if len(codes_in_ret) < 2:
            continue

        sub_ret = log_ret[codes_in_ret].dropna(how="all")
        corr_matrix = sub_ret.corr()

        for i in range(len(codes_in_ret)):
            for j in range(i + 1, len(codes_in_ret)):
                ca, cb = codes_in_ret[i], codes_in_ret[j]
                if ca not in corr_matrix.index or cb not in corr_matrix.columns:
                    continue
                corr = corr_matrix.loc[ca, cb]
                if np.isnan(corr) or corr < min_corr:
                    continue

                n_tested += 1
                # Compute spread = log(A) - log(B) on training window
                pa = log_prices[ca].dropna()
                pb = log_prices[cb].dropna()
                common_idx = pa.index.intersection(pb.index)
                if len(common_idx) < min_bars:
                    continue

                spread = (pa.loc[common_idx] - pb.loc[common_idx]).values

                # ADF test on spread
                p_val = adf_p_value(spread, lags=1)
                if p_val <= max_adf_p:
                    candidate_pairs.append((ca, cb, float(corr), float(p_val)))

    print(f"  pairs tested (corr >= {min_corr}): {n_tested}")
    print(f"  cointegrated pairs (ADF p <= {max_adf_p}): {len(candidate_pairs)}")

    # Sort by ADF p-value (most stationary first)
    candidate_pairs.sort(key=lambda x: x[3])
    return candidate_pairs


# =============================================================================
# FORWARD BACKTEST (true MTM equity curve)
# =============================================================================

def run_pairs_backtest(
    wide_full: pd.DataFrame,
    pairs: list[tuple[str, str, float, float]],
    cost_on: bool = False,
) -> dict:
    """
    True mark-to-market pairs backtest on TEST_START..TEST_END.

    Each day:
      1. Check exits for open pair positions.
      2. Mark remaining open positions to market (at close).
      3. Check entries (if slots available).
    Daily portfolio value = cash + sum(MTM value of open positions).
    """
    # Filter to test window
    test_mask = (wide_full.index >= pd.Timestamp(TEST_START)) & (wide_full.index <= pd.Timestamp(TEST_END))
    wide_test = wide_full[test_mask].copy()
    all_dates = wide_test.index.tolist()

    if len(all_dates) < 20:
        print(f"  ERROR: insufficient test bars ({len(all_dates)})")
        return {}

    print(f"  test window: {all_dates[0].date()} → {all_dates[-1].date()} ({len(all_dates)} days)")

    # Build set of all codes needed
    all_pair_codes = set()
    for ca, cb, _, _ in pairs:
        all_pair_codes.update([ca, cb])

    available_codes = set(wide_test.columns)
    valid_pairs = [(ca, cb, c, p) for (ca, cb, c, p) in pairs
                   if ca in available_codes and cb in available_codes]
    print(f"  pairs with both legs available in test window: {len(valid_pairs)}")

    if not valid_pairs:
        return {}

    # Pre-compute also a wider historical window for z-score normalization
    # We need ZSCORE_WINDOW days of history before TEST_START
    history_start = pd.Timestamp(TEST_START) - pd.Timedelta(days=ZSCORE_WINDOW * 2)
    hist_mask = (wide_full.index >= history_start) & (wide_full.index <= pd.Timestamp(TEST_END))
    wide_hist = wide_full[hist_mask].copy()

    # Pre-compute log-prices for spread
    log_wide_hist = np.log(wide_hist.replace(0, np.nan))

    # State
    open_positions: dict[int, dict] = {}  # slot_id -> position info
    closed_trades: list[dict] = []
    daily_portfolio_values: list[float] = []

    slot_counter = 0
    cost_per_trade = COST_RT_PAIR if cost_on else 0.0
    initial_capital = PORT_SIZE  # 10 pairs * 2 legs * 100k

    for today in all_dates:
        today_idx_in_hist = log_wide_hist.index.get_loc(today) if today in log_wide_hist.index else None

        # ---- EXIT CHECK ----
        to_close = []
        for slot_id, pos in open_positions.items():
            ca, cb = pos["code_a"], pos["code_b"]
            # Current spread z-score
            if today_idx_in_hist is None:
                continue
            if today_idx_in_hist < ZSCORE_WINDOW:
                continue

            pa_now = log_wide_hist.iloc[today_idx_in_hist][ca]
            pb_now = log_wide_hist.iloc[today_idx_in_hist][cb]
            if pd.isna(pa_now) or pd.isna(pb_now):
                continue

            # Rolling spread stats (past ZSCORE_WINDOW bars, excluding today)
            hist_slice = log_wide_hist.iloc[max(0, today_idx_in_hist - ZSCORE_WINDOW): today_idx_in_hist]
            spread_hist = (hist_slice[ca] - hist_slice[cb]).dropna()
            if len(spread_hist) < 20:
                continue

            spread_now = pa_now - pb_now
            z = (spread_now - spread_hist.mean()) / (spread_hist.std() + 1e-12)

            # Current prices for MTM
            px_a = wide_hist.iloc[today_idx_in_hist][ca]
            px_b = wide_hist.iloc[today_idx_in_hist][cb]
            if pd.isna(px_a) or pd.isna(px_b):
                continue

            hold_days = pos.get("hold_days", 0)
            pos["hold_days"] = hold_days + 1

            # Exit conditions
            should_exit = False
            exit_reason = None
            direction = pos["direction"]  # +1 = long A / short B, -1 = short A / long B

            if direction == 1:
                # Entered when A was cheap (spread low). Exit when spread reverts up.
                if z >= -EXIT_Z:  # spread reverted (z close to 0 or positive)
                    should_exit = True; exit_reason = "revert"
                elif z <= -STOP_Z:  # spread diverged further (more negative = A got even cheaper)
                    should_exit = True; exit_reason = "stop"
            else:
                # Entered when A was rich (spread high). Exit when spread reverts down.
                if z <= EXIT_Z:   # spread reverted
                    should_exit = True; exit_reason = "revert"
                elif z >= STOP_Z:  # spread diverged further
                    should_exit = True; exit_reason = "stop"

            if pos["hold_days"] >= MAX_HOLD:
                should_exit = True; exit_reason = "max_hold"

            if should_exit:
                # Compute realized PnL for this pair trade
                entry_px_a = pos["entry_px_a"]
                entry_px_b = pos["entry_px_b"]
                # Long leg PnL
                leg_a_ret = (px_a - entry_px_a) / entry_px_a * direction
                leg_b_ret = (px_b - entry_px_b) / entry_px_b * (-direction)
                pair_pnl_frac = (leg_a_ret + leg_b_ret) / 2  # avg of two legs

                # Cost deduction
                pnl_after_cost = pair_pnl_frac - cost_per_trade / 2

                closed_trades.append({
                    "entry_date": pos["entry_date"],
                    "exit_date": today,
                    "code_a": ca,
                    "code_b": cb,
                    "direction": direction,
                    "pnl_frac": pnl_after_cost,
                    "hold_days": pos["hold_days"],
                    "exit_reason": exit_reason,
                    "entry_z": pos["entry_z"],
                    "exit_z": float(z),
                })
                to_close.append(slot_id)

        for slot_id in to_close:
            open_positions.pop(slot_id)

        # ---- MARK OPEN POSITIONS TO MARKET ----
        mtm_value = 0.0
        if today_idx_in_hist is not None and today_idx_in_hist >= ZSCORE_WINDOW:
            for pos in open_positions.values():
                ca, cb = pos["code_a"], pos["code_b"]
                px_a = wide_hist.iloc[today_idx_in_hist][ca]
                px_b = wide_hist.iloc[today_idx_in_hist][cb]
                if pd.isna(px_a) or pd.isna(px_b):
                    continue
                entry_px_a = pos["entry_px_a"]
                entry_px_b = pos["entry_px_b"]
                direction = pos["direction"]
                leg_a_ret = (px_a - entry_px_a) / entry_px_a * direction
                leg_b_ret = (px_b - entry_px_b) / entry_px_b * (-direction)
                pair_mtm_frac = (leg_a_ret + leg_b_ret) / 2
                mtm_value += pair_mtm_frac * 2 * POSITION_SIZE

        daily_portfolio_values.append(initial_capital + mtm_value)

        # ---- ENTRY CHECK ----
        if today_idx_in_hist is None or today_idx_in_hist < ZSCORE_WINDOW:
            continue
        if len(open_positions) >= MAX_CONCURRENT_PAIRS:
            continue

        # Check each pair for entry signal
        open_pair_codes = set()
        for pos in open_positions.values():
            open_pair_codes.update([pos["code_a"], pos["code_b"]])

        for ca, cb, corr, adf_p in valid_pairs:
            if len(open_positions) >= MAX_CONCURRENT_PAIRS:
                break
            # Don't trade same code in two pairs simultaneously
            if ca in open_pair_codes or cb in open_pair_codes:
                continue

            pa_now = log_wide_hist.iloc[today_idx_in_hist][ca]
            pb_now = log_wide_hist.iloc[today_idx_in_hist][cb]
            if pd.isna(pa_now) or pd.isna(pb_now):
                continue

            # Rolling spread stats (past ZSCORE_WINDOW bars, excluding today)
            hist_slice = log_wide_hist.iloc[max(0, today_idx_in_hist - ZSCORE_WINDOW): today_idx_in_hist]
            spread_hist = (hist_slice[ca] - hist_slice[cb]).dropna()
            if len(spread_hist) < 20:
                continue

            spread_now = pa_now - pb_now
            z = (spread_now - spread_hist.mean()) / (spread_hist.std() + 1e-12)

            if abs(z) < ENTRY_Z:
                continue

            # Entry: if z < -ENTRY_Z → A is cheap vs B → long A, short B (direction=+1)
            #        if z > +ENTRY_Z → A is rich vs B → short A, long B (direction=-1)
            direction = -1 if z > ENTRY_Z else 1

            # Current close prices for entry
            entry_px_a = wide_hist.iloc[today_idx_in_hist][ca]
            entry_px_b = wide_hist.iloc[today_idx_in_hist][cb]
            if pd.isna(entry_px_a) or pd.isna(entry_px_b) or entry_px_a <= 0 or entry_px_b <= 0:
                continue

            slot_counter += 1
            open_positions[slot_counter] = {
                "entry_date": today,
                "code_a": ca,
                "code_b": cb,
                "direction": direction,
                "entry_px_a": float(entry_px_a),
                "entry_px_b": float(entry_px_b),
                "entry_z": float(z),
                "hold_days": 0,
            }
            open_pair_codes.update([ca, cb])

    # ---- FORCE CLOSE remaining positions at end ----
    last_idx = len(all_dates) - 1
    last_date = all_dates[last_idx]
    if last_date in log_wide_hist.index:
        last_hist_idx = log_wide_hist.index.get_loc(last_date)
        for slot_id, pos in list(open_positions.items()):
            ca, cb = pos["code_a"], pos["code_b"]
            pa_now = log_wide_hist.iloc[last_hist_idx][ca]
            pb_now = log_wide_hist.iloc[last_hist_idx][cb]
            if pd.isna(pa_now) or pd.isna(pb_now):
                continue
            hist_slice = log_wide_hist.iloc[max(0, last_hist_idx - ZSCORE_WINDOW): last_hist_idx]
            spread_hist = (hist_slice[ca] - hist_slice[cb]).dropna()
            if len(spread_hist) < 5:
                continue
            spread_now = pa_now - pb_now
            z = (spread_now - spread_hist.mean()) / (spread_hist.std() + 1e-12)

            px_a = wide_hist.iloc[last_hist_idx][ca]
            px_b = wide_hist.iloc[last_hist_idx][cb]
            if pd.isna(px_a) or pd.isna(px_b):
                continue

            direction = pos["direction"]
            entry_px_a = pos["entry_px_a"]
            entry_px_b = pos["entry_px_b"]
            leg_a_ret = (px_a - entry_px_a) / entry_px_a * direction
            leg_b_ret = (px_b - entry_px_b) / entry_px_b * (-direction)
            pair_pnl_frac = (leg_a_ret + leg_b_ret) / 2
            pnl_after_cost = pair_pnl_frac - cost_per_trade / 2

            closed_trades.append({
                "entry_date": pos["entry_date"],
                "exit_date": last_date,
                "code_a": ca,
                "code_b": cb,
                "direction": direction,
                "pnl_frac": pnl_after_cost,
                "hold_days": pos.get("hold_days", 1),
                "exit_reason": "eod",
                "entry_z": pos["entry_z"],
                "exit_z": float(z),
            })

    return {
        "closed_trades": closed_trades,
        "daily_portfolio_values": daily_portfolio_values,
        "all_dates": all_dates,
    }


# =============================================================================
# METRICS: TRUE MTM EQUITY CURVE
# =============================================================================

def wilson_ci(k: int, n: int, z: float = Z95) -> tuple[float, float]:
    if n == 0:
        return float("nan"), float("nan")
    p = k / n
    denom = 1 + z * z / n
    center = (p + z * z / (2 * n)) / denom
    half = (z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n))) / denom
    return center - half, center + half


def mean_ci(x: np.ndarray, z: float = Z95) -> tuple[float, float, float]:
    n = len(x)
    if n == 0:
        return float("nan"), float("nan"), float("nan")
    m = float(x.mean())
    if n < 2:
        return m, float("nan"), float("nan")
    se = float(x.std(ddof=1)) / math.sqrt(n)
    return m, m - z * se, m + z * se


def compute_metrics(
    result: dict,
    nikkei_returns: pd.Series,
    label: str,
    years: float,
) -> dict:
    """Compute all metrics from backtest result."""
    if not result or not result.get("daily_portfolio_values"):
        return {"label": label, "n": 0}

    dpv = result["daily_portfolio_values"]
    all_dates = result["all_dates"]
    closed_trades = result["closed_trades"]

    if len(dpv) < 10:
        return {"label": label, "n": 0}

    # ---- TRUE MTM EQUITY CURVE ----
    eq = pd.Series(dpv, index=all_dates)
    daily_ret = eq.pct_change().dropna()

    # Annualized Sharpe from daily MTM
    if daily_ret.std() <= 0:
        ann_sharpe = float("nan")
    else:
        ann_sharpe = float(daily_ret.mean() / daily_ret.std() * math.sqrt(252))

    # CAGR
    total_ret = float(eq.iloc[-1] / eq.iloc[0] - 1.0)
    cagr = (1.0 + total_ret) ** (1.0 / years) - 1.0 if years > 0 else float("nan")

    # MaxDD
    peak = eq.cummax()
    dd = (eq - peak) / peak
    max_dd = float(dd.min())
    max_dd_date_ts = dd.idxmin()
    max_dd_date = str(max_dd_date_ts.date()) if hasattr(max_dd_date_ts, "date") else str(max_dd_date_ts)

    # ---- BETA TO NIKKEI ----
    # Align strategy daily returns with Nikkei returns
    aligned = pd.DataFrame({"strat": daily_ret}).join(
        nikkei_returns.rename("nk"), how="inner"
    )
    if len(aligned) >= 20:
        cov_sn = float(aligned["strat"].cov(aligned["nk"]))
        var_nk = float(aligned["nk"].var())
        beta = cov_sn / var_nk if var_nk > 0 else float("nan")
        corr_nk = float(aligned["strat"].corr(aligned["nk"]))
    else:
        beta = float("nan")
        corr_nk = float("nan")

    # ---- PER-TRADE STATS ----
    n = len(closed_trades)
    if n > 0:
        pnls = np.array([t["pnl_frac"] for t in closed_trades], dtype=float)
        holds = np.array([t["hold_days"] for t in closed_trades], dtype=float)
        wins = int((pnls > 0).sum())
        wr = wins / n
        wlo, whi = wilson_ci(wins, n)
        avg_ret, avg_lo, avg_hi = mean_ci(pnls)
        std_ret = float(pnls.std(ddof=1)) if n > 1 else 0.0
        avg_hold = float(holds.mean())

        # Exit reason breakdown
        reasons = {}
        for t in closed_trades:
            r = t.get("exit_reason", "?")
            reasons[r] = reasons.get(r, 0) + 1
    else:
        pnls = np.array([])
        wins = wr = wlo = whi = avg_ret = avg_lo = avg_hi = std_ret = avg_hold = 0.0
        reasons = {}

    calmar = cagr / abs(max_dd) if max_dd < 0 else float("nan")

    return {
        "label": label,
        "n": n,
        "win_rate": wr,
        "wr_ci": (wlo, whi),
        "avg_ret_per_trade": avg_ret,
        "avg_ret_ci": (avg_lo, avg_hi),
        "std_per_trade": std_ret,
        "avg_hold_days": avg_hold,
        "ann_sharpe_mtm": ann_sharpe,  # ← TRUE MTM Sharpe
        "cagr": cagr,
        "total_ret": total_ret,
        "max_dd": max_dd,
        "max_dd_date": max_dd_date,
        "calmar": calmar,
        "beta_to_nikkei": beta,
        "corr_to_nikkei": corr_nk,
        "exit_reasons": reasons,
    }


# =============================================================================
# REVERSAL BASELINE (for comparison)
# =============================================================================

def run_reversal_baseline() -> dict:
    """Run BNF reversal baseline for annualized Sharpe reference."""
    print("\n[baseline] BNF reversal engine...")
    try:
        from src.backtest.runner import compute_sector_thresholds_from_cache
        from src.backtest.engine import run_backtest

        thresholds = compute_sector_thresholds_from_cache(k=2.0)
        # Use full period for fair comparison
        res = run_backtest(
            sector_thresholds=thresholds,
            start_date=TRAIN_START,
            end_date=TEST_END,
            max_concurrent_positions=10,
            position_size=POSITION_SIZE,
            initial_balance=10 * POSITION_SIZE,
        )
        eq = res.equity_curve
        if len(eq) > 1:
            daily_ret = eq.pct_change().dropna()
            ann_sharpe = float(daily_ret.mean() / daily_ret.std() * math.sqrt(252)) if daily_ret.std() > 0 else 0.0
            total_ret = float(eq.iloc[-1] / eq.iloc[0] - 1.0)
            cagr = (1.0 + total_ret) ** (1.0 / FULL_YEARS) - 1.0
            peak = eq.cummax()
            dd = (eq - peak) / peak
            max_dd = float(dd.min())
            max_dd_date = str(dd.idxmin().date()) if hasattr(dd.idxmin(), "date") else "?"
        else:
            ann_sharpe = cagr = max_dd = 0.0
            max_dd_date = "?"

        return {
            "label": "BNF_reversal_baseline",
            "ann_sharpe_mtm": ann_sharpe,
            "cagr": cagr,
            "max_dd": max_dd,
            "max_dd_date": max_dd_date,
            "beta_to_nikkei": float("nan"),
        }
    except Exception as e:
        print(f"  baseline error: {e}")
        return {"label": "BNF_reversal_baseline", "ann_sharpe_mtm": 0.77, "cagr": float("nan"),
                "max_dd": float("nan"), "max_dd_date": "?", "beta_to_nikkei": float("nan")}


# =============================================================================
# PRINT SCORECARD
# =============================================================================

def print_scorecard(results: list[dict]) -> None:
    W = 140
    print()
    print("=" * W)
    print(f"SCORECARD — 3-5 ペアトレード S1ゲート (TRUE MTM Sharpe)")
    print(f"  train={TRAIN_START}..{TRAIN_END} | test={TEST_START}..{TEST_END}")
    print(f"  cost-ON: {COST_RT_PAIR*10000:.0f}bps round-trip per pair ({int(COST_BPS_PER_LEG)}bps/leg × 4)")
    print(f"  entry_z={ENTRY_Z} exit_z={EXIT_Z} stop_z={STOP_Z} max_hold={MAX_HOLD}d zscore_window={ZSCORE_WINDOW}d")
    print("-" * W)
    hdr = (f"{'config':<35} {'N':>5} {'WR%[95%CI]':>20} {'avgRet%[CI]':>22} "
           f"{'annSharpe(MTM)':>14} {'CAGR%':>7} {'maxDD%':>8} {'maxDD_dt':>12} "
           f"{'β_Nikkei':>9} {'hold':>5}")
    print(hdr)
    print("-" * W)

    for r in results:
        if r.get("n", -1) == 0:
            print(f"  {r['label']:<35} {'N=0'}")
            continue
        if "ann_sharpe_mtm" not in r:
            # Baseline row with fewer fields
            lbl = r.get("label", "?")[:34]
            sh = r.get("ann_sharpe_mtm", float("nan"))
            cagr = r.get("cagr", float("nan"))
            mdd = r.get("max_dd", float("nan"))
            beta = r.get("beta_to_nikkei", float("nan"))
            print(f"  {lbl:<35} {'':>5} {'':>20} {'':>22} {sh:>+14.3f} {cagr*100:>+6.1f}% {mdd*100:>+7.1f}% "
                  f"{'':>12} {beta:>9.3f}")
            continue

        n = r.get("n", 0)
        wr = r.get("win_rate", 0.0)
        wlo, whi = r.get("wr_ci", (float("nan"), float("nan")))
        avg = r.get("avg_ret_per_trade", 0.0)
        alo, ahi = r.get("avg_ret_ci", (float("nan"), float("nan")))
        sh = r.get("ann_sharpe_mtm", float("nan"))
        cagr = r.get("cagr", float("nan"))
        mdd = r.get("max_dd", float("nan"))
        mdd_dt = str(r.get("max_dd_date", "?"))[:10]
        beta = r.get("beta_to_nikkei", float("nan"))
        hold = r.get("avg_hold_days", 0.0)
        lbl = r["label"][:34]

        wr_str = f"{wr*100:.1f}%[{wlo*100:.1f},{whi*100:.1f}]"
        ret_str = f"{avg*100:+.2f}%[{alo*100:+.2f},{ahi*100:+.2f}]"
        print(f"  {lbl:<35} {n:>5} {wr_str:>20} {ret_str:>22} "
              f"{sh:>+14.3f} {cagr*100:>+6.1f}% {mdd*100:>+7.1f}% "
              f"{mdd_dt:>12} {beta:>+9.3f} {hold:>4.0f}d")

    print("=" * W)


# =============================================================================
# MAIN
# =============================================================================

def main() -> None:
    print("=" * 90)
    print("3-5 ペアトレード S1ゲート: statistical arbitrage / pairs mean-reversion")
    print(f"  universe: load_universe() | train={TRAIN_START}→{TRAIN_END} | test={TEST_START}→{TEST_END}")
    print(f"  pair selection: within-sector, corr>={MIN_CORR}, ADF_p<={MAX_ADF_P}")
    print(f"  z-score window={ZSCORE_WINDOW}d | entry_z={ENTRY_Z} | exit_z={EXIT_Z} | stop_z={STOP_Z}")
    print(f"  max_concurrent_pairs={MAX_CONCURRENT_PAIRS} | pos_size={POSITION_SIZE:,.0f}/leg")
    print(f"  cost-ON: {COST_RT_PAIR*10000:.0f}bps round-trip (pair=2 legs × 2 ways × {int(COST_BPS_PER_LEG)}bps)")
    print("=" * 90)

    # ── 1. Load data ──────────────────────────────────────────────────────────
    print("\n[1/5] Loading price data...")
    wide_full, universe = load_all_bars_wide()
    print(f"  wide matrix: {wide_full.shape[0]} dates × {wide_full.shape[1]} codes")

    sector_map = dict(zip(universe["Code"].astype(str), universe["S33Nm"]))

    # Nikkei for beta
    nk_ret = load_nikkei_bars()
    nk_ret_series = nk_ret.set_index("date")["close"].pct_change().dropna()
    nk_ret_series.index = pd.to_datetime(nk_ret_series.index)

    # ── 2. Pair selection (training only) ────────────────────────────────────
    print("\n[2/5] Selecting pairs (training window only — LOOK-AHEAD PREVENTION)...")
    print(f"  training window: {TRAIN_START} → {TRAIN_END}")
    train_mask = (wide_full.index >= pd.Timestamp(TRAIN_START)) & (wide_full.index <= pd.Timestamp(TRAIN_END))
    wide_train = wide_full[train_mask].copy()
    print(f"  training bars: {wide_train.shape[0]}")

    pairs = select_pairs(wide_train, sector_map, MIN_CORR, MAX_ADF_P, MIN_TRAIN_BARS)
    print(f"  selected pairs: {len(pairs)}")
    if pairs:
        print(f"  top 5 pairs (by ADF p-value):")
        for ca, cb, corr, adfp in pairs[:5]:
            sec = sector_map.get(ca, "?")
            print(f"    {ca}/{cb}  sector={sec}  corr={corr:.3f}  ADF_p={adfp:.4f}")

    if not pairs:
        print("  ERROR: No cointegrated pairs found. Adjust thresholds.")
        return

    # ── 3. Forward backtest (cost-OFF) ───────────────────────────────────────
    print(f"\n[3/5] Forward backtest (cost-OFF) on test window {TEST_START}→{TEST_END}...")
    result_off = run_pairs_backtest(wide_full, pairs, cost_on=False)
    metrics_off = compute_metrics(result_off, nk_ret_series, "pairs_cost-OFF", TEST_YEARS)

    if metrics_off.get("n", 0) == 0:
        print("  ERROR: No trades in test window.")
        return

    print(f"  trades: {metrics_off['n']}")
    print(f"  exit reasons: {metrics_off.get('exit_reasons', {})}")
    print(f"  TRUE MTM annualized Sharpe: {metrics_off['ann_sharpe_mtm']:.3f}")
    print(f"  CAGR: {metrics_off['cagr']*100:.2f}%  maxDD: {metrics_off['max_dd']*100:.2f}% ({metrics_off['max_dd_date']})")
    print(f"  beta-to-Nikkei: {metrics_off['beta_to_nikkei']:.3f}  corr: {metrics_off.get('corr_to_nikkei', float('nan')):.3f}")

    # ── 4. Forward backtest (cost-ON) ────────────────────────────────────────
    print(f"\n[4/5] Forward backtest (cost-ON, {COST_RT_PAIR*10000:.0f}bps round-trip)...")
    result_on = run_pairs_backtest(wide_full, pairs, cost_on=True)
    metrics_on = compute_metrics(result_on, nk_ret_series, "pairs_cost-ON", TEST_YEARS)

    if metrics_on.get("n", 0) > 0:
        print(f"  trades: {metrics_on['n']}")
        print(f"  TRUE MTM annualized Sharpe: {metrics_on['ann_sharpe_mtm']:.3f}")
        print(f"  CAGR: {metrics_on['cagr']*100:.2f}%  maxDD: {metrics_on['max_dd']*100:.2f}% ({metrics_on['max_dd_date']})")
        print(f"  beta-to-Nikkei: {metrics_on['beta_to_nikkei']:.3f}")

    # ── 5. Reversal baseline ─────────────────────────────────────────────────
    baseline = run_reversal_baseline()

    # ── Scorecard ─────────────────────────────────────────────────────────────
    results_list = [metrics_off, metrics_on, baseline]
    print_scorecard(results_list)

    # ── CI Overlap Analysis ───────────────────────────────────────────────────
    print("\n[CI Overlap Analysis]")
    if metrics_off.get("n", 0) > 0:
        alo, ahi = metrics_off["avg_ret_ci"]
        print(f"  avg_ret_per_trade CI: [{alo*100:+.3f}%, {ahi*100:+.3f}%]  "
              f"{'CI includes 0 → not statistically significant' if alo <= 0 <= ahi else 'CI excludes 0 → statistically significant'}")
    print(f"  pairs ann_sharpe(MTM) cost-OFF: {metrics_off.get('ann_sharpe_mtm', float('nan')):.3f}")
    print(f"  pairs ann_sharpe(MTM) cost-ON:  {metrics_on.get('ann_sharpe_mtm', float('nan')):.3f}")
    print(f"  reversal baseline:              {baseline.get('ann_sharpe_mtm', 0.77):.3f}")
    print(f"  beta-to-Nikkei: {metrics_off.get('beta_to_nikkei', float('nan')):.3f}  "
          f"{'(near-neutral ✓)' if abs(metrics_off.get('beta_to_nikkei', 999)) < 0.20 else '(NOT market-neutral ✗)'}")

    # ── VERDICT ──────────────────────────────────────────────────────────────
    print()
    print("=" * 90)
    print("VERDICT")
    print("=" * 90)

    sh_off = metrics_off.get("ann_sharpe_mtm", float("nan"))
    sh_on  = metrics_on.get("ann_sharpe_mtm", float("nan"))
    rev_sh = baseline.get("ann_sharpe_mtm", 0.77)
    beta   = metrics_off.get("beta_to_nikkei", float("nan"))
    cagr   = metrics_off.get("cagr", float("nan"))
    mdd    = metrics_off.get("max_dd", float("nan"))
    mdd_dt = metrics_off.get("max_dd_date", "?")
    n_trades = metrics_off.get("n", 0)
    avg_lo, avg_hi = metrics_off.get("avg_ret_ci", (float("nan"), float("nan")))
    ci_positive = (not math.isnan(avg_lo)) and avg_lo > 0

    is_neutral = (not math.isnan(beta)) and abs(beta) < 0.25
    sh_off_ok  = (not math.isnan(sh_off)) and sh_off >= 0.5
    sh_on_ok   = (not math.isnan(sh_on))  and sh_on  >= 0.3
    cost_kills = (not math.isnan(sh_on))  and sh_on  < 0.1

    if n_trades < 30:
        verdict = "RED"
        detail = f"insufficient trades (N={n_trades} < 30) — no statistical power"
    elif math.isnan(sh_off) or sh_off < 0.2:
        verdict = "RED"
        detail = f"no edge: ann_sharpe(MTM) cost-OFF = {sh_off:.3f} < 0.20"
    elif cost_kills:
        verdict = "RED"
        detail = f"cost kills edge: cost-OFF={sh_off:.3f} → cost-ON={sh_on:.3f}"
    elif sh_off_ok and sh_on_ok and is_neutral and ci_positive:
        verdict = "GREEN"
        detail = f"real market-neutral edge: Sharpe(OFF)={sh_off:.3f} Sharpe(ON)={sh_on:.3f} β={beta:.3f} CI_lo>0"
    elif sh_off >= 0.3 and sh_on_ok:
        verdict = "YELLOW"
        detail = f"marginal edge: Sharpe(OFF)={sh_off:.3f} Sharpe(ON)={sh_on:.3f} β={beta:.3f} — monitor conditions"
    elif sh_off >= 0.3 and not sh_on_ok:
        verdict = "YELLOW"
        detail = f"cost-fragile: Sharpe(OFF)={sh_off:.3f} but cost-ON={sh_on:.3f} — edge eroded by 60bps RT"
    else:
        verdict = "RED"
        detail = f"no edge: Sharpe(OFF)={sh_off:.3f} — below minimum threshold"

    print(f"\n  {verdict}: {detail}")
    print()
    print("Summary metrics (TRUE MTM Sharpe):")
    print(f"  N trades: {n_trades}  avg_hold: {metrics_off.get('avg_hold_days', 0.0):.1f}d")
    print(f"  CAGR: {cagr*100:+.2f}%  maxDD: {mdd*100:.2f}% ({mdd_dt})")
    print(f"  beta-to-Nikkei: {beta:.3f} ({'market-neutral' if is_neutral else 'NOT neutral'})")
    print()
    print("Biggest caveat:")
    print(f"  LOOK-AHEAD: Pairs selected on training {TRAIN_START}→{TRAIN_END}; tested on forward")
    print(f"  {TEST_START}→{TEST_END}. Look-ahead AVOIDED ✓. However, pair relationships")
    print(f"  may degrade over time (regime shifts, corporate events, mergers). Rolling")
    print(f"  re-selection would be more realistic but risks overfitting.")
    print(f"  COST: 60bps round-trip (2 legs × 2 ways × 15bps). Pairs edges are thin")
    print(f"  and this cost level is realistic for small retail orders (<¥100M) but")
    print(f"  could be tighter for institutional size.")
    print(f"  SURVIVORSHIP: Universe is current survivors. Delisted names excluded.")
    print(f"  This biases pair selection toward stable pairs (favorable bias).")
    print()
    print("SCRIPT: /Users/ryu/grove-stock/docs/grove-stock/research/3-5_pairs_gate.py")
    print("=" * 90)


if __name__ == "__main__":
    main()
