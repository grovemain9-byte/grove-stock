"""バックテストエンジン基本テスト。
キャッシュ依存のためpytest markerで分離。
"""
from __future__ import annotations

import pytest
import pandas as pd

from src.backtest.engine import _compute_indicators, MA_PERIOD


def test_compute_indicators_basic():
    """指標計算: MA25, deviation, RSI, BB_lower が生成されるか。"""
    # 30日分の合成データ
    dates = pd.date_range("2026-01-01", periods=30, freq="D").date
    prices = [100 + i * 0.5 for i in range(30)]  # 上昇トレンド
    df = pd.DataFrame({
        "date": dates, "open": prices, "high": [p+1 for p in prices],
        "low": [p-1 for p in prices], "close": prices, "volume": [1000]*30,
    })
    out = _compute_indicators(df)
    assert "ma25" in out.columns
    assert "deviation" in out.columns
    assert "rsi" in out.columns
    assert "bb_lower" in out.columns
    # MA25は25日目以降に値が入る
    assert not pd.isna(out["ma25"].iloc[-1])
    # 上昇トレンドなので deviation > 0
    assert out["deviation"].iloc[-1] > 0


def test_compute_indicators_volume_trend():
    """出来高減少トレンドがBooleanで検出されるか。"""
    dates = pd.date_range("2026-01-01", periods=30, freq="D").date
    volumes = [1000 - i*10 for i in range(30)]  # 減少
    df = pd.DataFrame({
        "date": dates, "open": [100]*30, "high": [101]*30,
        "low": [99]*30, "close": [100]*30, "volume": volumes,
    })
    out = _compute_indicators(df)
    # 最終行 = 直近2日間が減少 = True
    assert out["vol_decreasing"].iloc[-1] == True


def test_ema900_slope_up_returns_bool_not_nan_in_warmup():
    """ema900_slope_up = (ema900 > ema900.shift(20)) は pandas 規約で NaN を False に解決する。

    過去 bug (engine.py:219 で `df['ema900_slope_up'].notna().any()` を使い、警戒期不足を
    検出できなかった) の regression test。警戒期(<900 bars)では:
      - ema900 は全部 NaN (min_periods=900)
      - ema900_slope_up は全部 False (bool・non-NaN)
    したがって warmup honesty 検証は ema900 自体の notna を見るべき。
    """
    # 50 bars only → ema900 は全部 NaN になるはず
    dates = pd.date_range("2026-01-01", periods=50, freq="D").date
    df = pd.DataFrame({
        "date": dates, "open": [100.0] * 50, "high": [101.0] * 50,
        "low": [99.0] * 50, "close": [100.0] * 50, "volume": [1000] * 50,
    })
    out = _compute_indicators(df)
    # ema900 自体は警戒期不足で全部 NaN
    assert out["ema900"].isna().all(), "ema900 should be all NaN with only 50 bars"
    # ema900_slope_up は bool 比較で False に解決（NaN ではない）
    assert out["ema900_slope_up"].notna().all(), (
        "ema900_slope_up should be all bool (False), NOT NaN, due to pandas NaN-comparison semantics"
    )
    assert (out["ema900_slope_up"] == False).all()  # noqa: E712
    # 正しい warmup 判定は ema900.notna() で行う
    assert not out["ema900"].notna().any()
