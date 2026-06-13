"""S4: empyrical-reloaded 導入の smoke + engine 整合クロスチェック。

plan: engine.metrics() は ground truth で温存。empyrical は「追加レンズ」。
両者の maxDD が同一データで一致することを証明＝empyrical を計測層に足しても
真実源と矛盾しない（S5 walk_forward で安心して併記できる前提）。
"""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

import empyrical


def _engine_style_mdd(equity: pd.Series) -> float:
    """engine.BacktestResult.metrics() と同一の maxDD 式（equity直計算）。"""
    peak = equity.cummax()
    dd = (equity - peak) / peak
    return float(dd.min())


def test_empyrical_importable_and_pinned():
    # 無印 empyrical(dead 2020) でなく reloaded が入っていること
    assert empyrical.__version__ == "0.5.12"
    assert hasattr(empyrical, "max_drawdown")
    assert hasattr(empyrical, "sharpe_ratio")


def test_maxdd_matches_engine_formula_synthetic():
    """合成 equity: empyrical.max_drawdown(returns) == engine equity式 maxDD。"""
    eq = pd.Series([100, 110, 121, 99, 90, 108, 130, 117, 140], dtype=float)
    engine_mdd = _engine_style_mdd(eq)
    emp_mdd = float(empyrical.max_drawdown(eq.pct_change().dropna()))
    assert emp_mdd == pytest.approx(engine_mdd, abs=1e-12)
    assert engine_mdd < 0  # 実際にDDが出る系列で検証している保証


def test_maxdd_matches_on_random_walks():
    """複数のランダムウォークでも一致（偶然一致でないことの担保）。"""
    rng = np.random.default_rng(42)
    for _ in range(5):
        eq = pd.Series(1000 * np.cumprod(1 + rng.normal(0, 0.02, 250)))
        assert float(empyrical.max_drawdown(eq.pct_change().dropna())) == pytest.approx(
            _engine_style_mdd(eq), abs=1e-12
        )


@pytest.mark.integration
def test_empyrical_matches_engine_on_real_backtest():
    """実 3年バックテストの equity_curve で engine.metrics() maxDD と一致。

    engine が真実源。empyrical はその曲線に対する追加レンズで矛盾しないこと。
    """
    from src.backtest.runner import compute_sector_thresholds_from_cache
    from src.backtest.engine import run_backtest

    th = compute_sector_thresholds_from_cache(k=2.0)
    res = run_backtest(sector_thresholds=th, max_concurrent_positions=7,
                       consensus_min=3, p4_required=False, p4_threshold=0.0)
    m = res.metrics()
    eq = res.equity_curve
    assert len(eq) > 0
    emp_mdd = float(empyrical.max_drawdown(eq.pct_change().dropna()))
    # engine.metrics()['max_drawdown'] は同一 equity の cummax 式
    assert emp_mdd == pytest.approx(m["max_drawdown"], abs=1e-9)
    # baseline 値（2026-06-13 更新）。
    # src/data 7ファイルがgitignoreバグで未追跡だったため回収後に再計測。
    # -0.23020729939409995 (旧) → -0.22408126251239374 (新)。
    assert m["max_drawdown"] == pytest.approx(-0.22408126251239374, abs=1e-9)
