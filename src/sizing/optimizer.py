"""EVS weight optimizer — scipy で walk-forward Sharpe を最大化.

Phase 1 (Grove 2026-05-21):
- decision_shadow から (features, cf_pnl_pct) を抽出
- scipy.optimize.minimize で w1..w5 (EVS signal weights) を学習
- purged walk-forward CV で out-of-sample 検証
- 最適重みを config/strategy_params.py or json に書き戻し

Notes:
- Phase 0+ の WEIGHTS は init として使う
- 走らせる頻度: 週次 (weekly_retrain.py) or n+10 trades 増えたら
- データ不足 (n<30) なら skip → log warning
- 制約: w_i ∈ [0, 1], sum(w) = 1.0
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, asdict
from datetime import date, datetime
from pathlib import Path
from typing import Optional

import duckdb
import numpy as np
import pandas as pd
from scipy.optimize import minimize

from src.sizing.evs import WEIGHTS as INITIAL_WEIGHTS

logger = logging.getLogger("sizing.optimizer")

MIN_SAMPLES_FOR_OPTIMIZATION = 30  # データ不足ならskip
WALK_FORWARD_N_SPLITS = 5
EMBARGO_DAYS = 5  # max_hold_days と整合

# Optimizer bounds and constraints (9 factors since 2026-05-22 all-additive)
WEIGHT_BOUNDS = [(0.0, 1.0)] * 9
WEIGHT_SUM = 1.0  # equality constraint

# Sharpe annualization (daily-ish; trades happen on entry-day evaluation)
ANNUALIZATION_FACTOR = np.sqrt(252)


@dataclass(frozen=True)
class OptimizationResult:
    n_samples: int
    n_folds: int
    initial_weights: dict[str, float]
    optimal_weights: dict[str, float]
    initial_sharpe: float
    optimal_sharpe: float
    improvement: float
    converged: bool
    timestamp: datetime

    def as_dict(self) -> dict:
        d = asdict(self)
        d["timestamp"] = d["timestamp"].isoformat()
        return d


# 9-factor order (matches evs.DEFAULT_WEIGHTS keys; 2026-05-22 all-additive刷新)
FACTOR_KEYS = ["F1", "F2", "F3", "F4", "F5", "F6", "F7", "F8", "F10"]
FACTOR_JSON_FIELDS = [
    "f1_signal_strength", "f2_deviation_depth", "f3_rsi_oversold",
    "f4_bb_penetration", "f5_volume_decline", "f6_bayes_winrate",
    "f7_market_regime", "f8_realized_vol_filter", "f10_liquidity_filter",
]
N_FACTORS = len(FACTOR_KEYS)


def _load_resolved_trades(con: duckdb.DuckDBPyConnection) -> pd.DataFrame:
    """Pull resolved (cf_pnl_pct set) trades with their EVS feature JSON."""
    df = con.execute(
        """
        SELECT decided_date, ticker, evs_components_json, cf_pnl_pct, cf_won
        FROM decision_shadow
        WHERE cf_pnl_pct IS NOT NULL
          AND evs_components_json IS NOT NULL
          AND decision = 'go'
        ORDER BY decided_date
        """
    ).fetchdf()
    if df.empty:
        return df
    # Parse JSON into 9 factor columns
    parsed = df["evs_components_json"].apply(lambda s: json.loads(s) if s else {})
    for f in FACTOR_JSON_FIELDS:
        df[f] = parsed.apply(lambda d: d.get(f, 0.0))
    return df


def _evs_from_weights(df: pd.DataFrame, w: np.ndarray) -> np.ndarray:
    """Recompute additive 9-factor score per row given weights."""
    w_sum = w.sum()
    if w_sum <= 0:
        return np.zeros(len(df))
    acc = np.zeros(len(df))
    for i, field in enumerate(FACTOR_JSON_FIELDS):
        acc = acc + w[i] * df[field].to_numpy()
    return acc / w_sum


def _sharpe(returns: np.ndarray) -> float:
    """Annualized Sharpe ratio. NaN-safe."""
    r = returns[~np.isnan(returns)]
    if len(r) < 2:
        return 0.0
    std = r.std(ddof=1)
    if std <= 1e-9:
        return 0.0
    return float(r.mean() / std * ANNUALIZATION_FACTOR)


def _walk_forward_score(
    df: pd.DataFrame,
    w: np.ndarray,
    *,
    n_splits: int = WALK_FORWARD_N_SPLITS,
    embargo: int = EMBARGO_DAYS,
) -> float:
    """For given weights, score = mean OOS Sharpe across walk-forward folds.

    Each fold trains nothing (weights are exogenous) but evaluates the
    "edge_score weighted by w → predicted return" relationship on OOS slice.
    Score = correlation between weighted edge_score and actual cf_pnl_pct,
    weighted by sample size, on OOS. Higher = better discrimination.
    """
    n = len(df)
    if n < (n_splits + 1) + embargo:
        return 0.0

    # Sort by date for purged WF
    df_sorted = df.sort_values("decided_date").reset_index(drop=True)
    bounds = np.linspace(0, n, n_splits + 1, dtype=int)

    fold_returns: list[float] = []
    for k in range(1, n_splits + 1):
        cut = bounds[k]
        nxt = bounds[k + 1] if k < n_splits else n
        # train: [0, cut-embargo)  test: [cut, nxt)
        train_end = cut - embargo
        if train_end <= 0 or nxt <= cut:
            continue
        test_slice = df_sorted.iloc[cut:nxt]
        if len(test_slice) == 0:
            continue
        # Strategy: take top half edge_score quantile, see avg cf_pnl
        edge = _evs_from_weights(test_slice, w)
        # Strategy return: weighted average of cf_pnl_pct by edge_score (long-only)
        # = sum(edge * pnl) / sum(edge), but if all edge=0 then fall back to mean
        pnls = test_slice["cf_pnl_pct"].to_numpy()
        if edge.sum() <= 1e-9:
            fold_avg = float(np.nanmean(pnls)) if len(pnls) > 0 else 0.0
        else:
            fold_avg = float(np.nansum(edge * pnls) / edge.sum())
        fold_returns.append(fold_avg)

    if not fold_returns:
        return 0.0
    # Aggregate fold-level returns into Sharpe-like metric
    return _sharpe(np.array(fold_returns))


def optimize_weights(
    db_path: str,
    *,
    initial: Optional[dict[str, float]] = None,
    n_splits: int = WALK_FORWARD_N_SPLITS,
    verbose: bool = True,
) -> Optional[OptimizationResult]:
    """Run scipy.optimize on EVS weights.

    Returns None if data is insufficient (logs a warning).
    """
    con = duckdb.connect(db_path, read_only=True)
    try:
        df = _load_resolved_trades(con)
    finally:
        con.close()

    n = len(df)
    if verbose:
        print(f"[optimizer] loaded {n} resolved trades")

    if n < MIN_SAMPLES_FOR_OPTIMIZATION:
        logger.warning(
            "Insufficient samples for optimization: %d < %d. Skip.",
            n, MIN_SAMPLES_FOR_OPTIMIZATION,
        )
        if verbose:
            print(f"[optimizer] SKIP: need >= {MIN_SAMPLES_FOR_OPTIMIZATION}, have {n}")
        return None

    initial = initial or INITIAL_WEIGHTS
    w0 = np.array([initial.get(k, 1.0 / N_FACTORS) for k in FACTOR_KEYS])
    # Normalize initial sum to 1
    if w0.sum() > 0:
        w0 = w0 / w0.sum()

    initial_score = _walk_forward_score(df, w0, n_splits=n_splits)

    # scipy.optimize: minimize negative score
    def neg_score(w: np.ndarray) -> float:
        return -_walk_forward_score(df, w, n_splits=n_splits)

    constraints = [{"type": "eq", "fun": lambda w: w.sum() - WEIGHT_SUM}]
    result = minimize(
        neg_score, w0, method="SLSQP",
        bounds=WEIGHT_BOUNDS, constraints=constraints,
        options={"maxiter": 100, "ftol": 1e-6},
    )

    optimal_w = result.x
    # Ensure non-negative and re-normalize after SLSQP rounding
    optimal_w = np.clip(optimal_w, 0.0, 1.0)
    if optimal_w.sum() > 0:
        optimal_w = optimal_w / optimal_w.sum()
    optimal_score = _walk_forward_score(df, optimal_w, n_splits=n_splits)

    out = OptimizationResult(
        n_samples=n,
        n_folds=n_splits,
        initial_weights={FACTOR_KEYS[i]: float(w0[i]) for i in range(N_FACTORS)},
        optimal_weights={FACTOR_KEYS[i]: float(optimal_w[i]) for i in range(N_FACTORS)},
        initial_sharpe=initial_score,
        optimal_sharpe=optimal_score,
        improvement=optimal_score - initial_score,
        converged=bool(result.success),
        timestamp=datetime.now(),
    )

    if verbose:
        print(f"[optimizer] initial weights: {out.initial_weights}")
        print(f"[optimizer] initial OOS Sharpe: {out.initial_sharpe:+.4f}")
        print(f"[optimizer] optimal weights: {out.optimal_weights}")
        print(f"[optimizer] optimal OOS Sharpe: {out.optimal_sharpe:+.4f}")
        print(f"[optimizer] improvement: {out.improvement:+.4f}")
        print(f"[optimizer] converged: {out.converged}")
    return out


def persist_weights(result: OptimizationResult, json_path: str | Path) -> None:
    """Write optimized weights to a JSON file (read by evs.py at startup)."""
    path = Path(json_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "weights": result.optimal_weights,
        "metadata": {
            "n_samples": result.n_samples,
            "optimal_sharpe": result.optimal_sharpe,
            "improvement": result.improvement,
            "converged": result.converged,
            "timestamp": result.timestamp.isoformat(),
        },
    }
    path.write_text(json.dumps(payload, indent=2))
    logger.info("Persisted optimal weights to %s", path)
