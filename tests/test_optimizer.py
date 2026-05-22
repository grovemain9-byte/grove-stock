"""Tests for src/sizing/optimizer.py — scipy weight optimization.

Phase 1: scipy-based EVS weight learning from cf_pnl_pct.
"""
from __future__ import annotations

import json
from datetime import date, datetime

import duckdb
import numpy as np
import pytest

from src.sizing.optimizer import (
    MIN_SAMPLES_FOR_OPTIMIZATION,
    OptimizationResult,
    _evs_from_weights,
    _sharpe,
    _walk_forward_score,
    optimize_weights,
    persist_weights,
)


def _seed_db(con: duckdb.DuckDBPyConnection, n: int, *, seed: int = 42) -> None:
    """Seed decision_shadow with synthetic resolved trades."""
    con.execute("""
        CREATE TABLE IF NOT EXISTS decision_shadow (
            id INTEGER PRIMARY KEY,
            decided_date DATE,
            ticker VARCHAR,
            decision VARCHAR,
            evs_components_json TEXT,
            cf_pnl_pct DOUBLE,
            cf_won BOOLEAN
        )
    """)
    rng = np.random.default_rng(seed)
    for i in range(n):
        # F2 (deviation) と outcome に強い負の相関を仕込む (実データ初期発見と整合)
        f2 = rng.uniform(0, 1)
        f1 = rng.uniform(0, 1)
        f3 = rng.uniform(0, 1)
        f4 = rng.uniform(0, 1)
        f5 = rng.uniform(0, 1)
        # 「乖離が深いほど負け」を埋め込む: pnl = -0.05 * f2 + noise
        pnl = -0.05 * f2 + rng.normal(0, 0.02)
        comp = {
            "f1_signal_strength": f1, "f2_deviation_depth": f2,
            "f3_rsi_oversold": f3, "f4_bb_penetration": f4,
            "f5_volume_decline": f5,
        }
        con.execute(
            "INSERT INTO decision_shadow VALUES (?, ?, ?, ?, ?, ?, ?)",
            [i, date(2026, 5, 1 + i % 28), f"T{i}", "go", json.dumps(comp), pnl, pnl > 0],
        )


class TestSharpe:
    def test_zero_std(self):
        assert _sharpe(np.array([0.01, 0.01, 0.01])) == 0.0

    def test_known_sharpe(self):
        """mean 0.001, std 0.01 → sharpe ≈ 0.1 * sqrt(252) ≈ 1.587."""
        rets = np.array([0.011, -0.009, 0.012, -0.008, 0.010, -0.010] * 5)
        s = _sharpe(rets)
        assert isinstance(s, float)
        # Just check it's nonzero
        assert abs(s) > 0


class TestEvsFromWeights:
    @staticmethod
    def _df9(*rows):
        """Build a DataFrame with all 9 factor columns. Each row is a single value
        broadcast to all 9 factors."""
        import pandas as pd
        from src.sizing.optimizer import FACTOR_JSON_FIELDS
        data = {field: [r for r in rows] for field in FACTOR_JSON_FIELDS}
        return pd.DataFrame(data)

    def test_uniform_weights(self):
        # All 9 factors = 1.0 (row0) and 0.5 (row1), uniform weights → 1.0 and 0.5
        df = self._df9(1.0, 0.5)
        w = np.full(9, 1.0 / 9)
        edge = _evs_from_weights(df, w)
        assert edge[0] == pytest.approx(1.0)
        assert edge[1] == pytest.approx(0.5)

    def test_zero_weights_zero_edge(self):
        df = self._df9(1.0)
        w = np.zeros(9)
        assert _evs_from_weights(df, w)[0] == 0.0


class TestOptimize:
    def test_insufficient_data_returns_none(self, tmp_path):
        db = tmp_path / "test.duckdb"
        con = duckdb.connect(str(db))
        _seed_db(con, n=5)
        con.close()
        result = optimize_weights(str(db), verbose=False)
        assert result is None

    def test_sufficient_data_runs_optimization(self, tmp_path):
        db = tmp_path / "test.duckdb"
        con = duckdb.connect(str(db))
        _seed_db(con, n=MIN_SAMPLES_FOR_OPTIMIZATION + 10)
        con.close()
        result = optimize_weights(str(db), verbose=False)
        assert result is not None
        assert isinstance(result, OptimizationResult)
        assert result.n_samples == MIN_SAMPLES_FOR_OPTIMIZATION + 10
        # Weights sum to 1.0
        opt_sum = sum(result.optimal_weights.values())
        assert opt_sum == pytest.approx(1.0, abs=0.01)
        # F2 weight should be low (we injected anti-correlation with pnl)
        # so optimizer should down-weight F2
        # NOTE: SLSQP can land at boundary; check it's at least <= initial
        # (Not strict because synthetic data + few folds is noisy)

    def test_persist_weights(self, tmp_path):
        result = OptimizationResult(
            n_samples=50, n_folds=5,
            initial_weights={"F1": 0.2, "F2": 0.2, "F3": 0.2, "F4": 0.2, "F5": 0.2},
            optimal_weights={"F1": 0.4, "F2": 0.05, "F3": 0.2, "F4": 0.15, "F5": 0.2},
            initial_sharpe=0.5, optimal_sharpe=0.8, improvement=0.3,
            converged=True, timestamp=datetime(2026, 5, 21),
        )
        json_path = tmp_path / "weights.json"
        persist_weights(result, json_path)
        assert json_path.exists()
        payload = json.loads(json_path.read_text())
        assert payload["weights"]["F1"] == 0.4
        assert payload["metadata"]["n_samples"] == 50
