"""Issue #3: データ取得・MA25計算テスト。

受け入れ基準:
- 13銘柄全件でfetchが正常完走すること
- MA25乖離率が数値として返ること（NaNでないこと）
- API接続できない場合に適切な例外を投げること
"""
import math
import os
from unittest.mock import patch, MagicMock

import numpy as np
import pandas as pd
import pytest

from src.data.jquants import (
    TICKERS,
    calc_ma25_deviation,
    fetch_daily_ohlcv,
    fetch_nikkei_change,
    fetch_all_tickers,
)


# --- ヘルパー ---

def _make_ohlcv_df(rows: int = 30, base_price: float = 1000.0) -> pd.DataFrame:
    dates = pd.bdate_range(end=pd.Timestamp.now(), periods=rows)
    prices = [base_price + i * 10 for i in range(rows)]
    return pd.DataFrame({
        "date": dates,
        "open": [p - 5 for p in prices],
        "high": [p + 20 for p in prices],
        "low": [p - 20 for p in prices],
        "close": prices,
        "volume": [100000] * rows,
    })


# --- calc_ma25_deviation ---

class TestCalcMA25Deviation:
    def test_returns_float(self):
        df = _make_ohlcv_df(30)
        result = calc_ma25_deviation(df)
        assert isinstance(result, float)
        assert not math.isnan(result)

    def test_positive_deviation(self):
        df = _make_ohlcv_df(30, base_price=1000.0)
        result = calc_ma25_deviation(df)
        assert result > 0

    def test_negative_deviation(self):
        prices = [2000.0 - i * 10 for i in range(30)]
        dates = pd.bdate_range(end=pd.Timestamp.now(), periods=30)
        df = pd.DataFrame({
            "date": dates, "open": prices, "high": prices,
            "low": prices, "close": prices, "volume": [100000] * 30,
        })
        result = calc_ma25_deviation(df)
        assert result < 0

    def test_flat_market_near_zero(self):
        dates = pd.bdate_range(end=pd.Timestamp.now(), periods=30)
        df = pd.DataFrame({
            "date": dates, "open": [1000.0] * 30, "high": [1000.0] * 30,
            "low": [1000.0] * 30, "close": [1000.0] * 30, "volume": [100000] * 30,
        })
        result = calc_ma25_deviation(df)
        assert abs(result) < 0.001

    def test_insufficient_data_raises(self):
        df = _make_ohlcv_df(10)
        with pytest.raises(ValueError, match="Need >=25"):
            calc_ma25_deviation(df)

    def test_deviation_magnitude(self):
        dates = pd.bdate_range(end=pd.Timestamp.now(), periods=30)
        closes = [1000.0] * 25 + [900.0] * 5
        df = pd.DataFrame({
            "date": dates, "open": closes, "high": closes,
            "low": closes, "close": closes, "volume": [100000] * 30,
        })
        result = calc_ma25_deviation(df)
        assert -0.10 < result < -0.05


# --- fetch_daily_ohlcv (Mocked — yfinance) ---

class TestFetchDailyOHLCV:
    @patch("src.data.jquants._fetch_ohlcv_yfinance")
    def test_returns_dataframe(self, mock_yf):
        mock_yf.return_value = _make_ohlcv_df(30)
        df = fetch_daily_ohlcv("4519")
        assert isinstance(df, pd.DataFrame)
        assert len(df) >= 25
        assert list(df.columns) == ["date", "open", "high", "low", "close", "volume"]

    @patch("src.data.jquants._jquants_enabled", return_value=False)
    @patch("src.data.jquants._fetch_ohlcv_yfinance")
    def test_empty_raises(self, mock_yf, mock_enabled):
        mock_yf.side_effect = ValueError("No yfinance data")
        with pytest.raises(ValueError):
            fetch_daily_ohlcv("4519")

    @patch("src.data.jquants._jquants_enabled", return_value=False)
    @patch("src.data.jquants._fetch_ohlcv_yfinance")
    def test_insufficient_rows_raises(self, mock_yf, mock_enabled):
        mock_yf.return_value = _make_ohlcv_df(10)
        with pytest.raises(ValueError, match="Insufficient"):
            fetch_daily_ohlcv("4519")

    @patch("src.data.jquants._jquants_enabled", return_value=True)
    @patch("src.data.jquants._fetch_ohlcv_jquants")
    def test_jquants_fallback_to_yfinance(self, mock_jq, mock_enabled):
        mock_jq.side_effect = RuntimeError("J-Quants expired")
        with patch("src.data.jquants._fetch_ohlcv_yfinance") as mock_yf:
            mock_yf.return_value = _make_ohlcv_df(30)
            df = fetch_daily_ohlcv("4519")
            assert len(df) >= 25
            mock_yf.assert_called_once()


# --- fetch_nikkei_change (Mocked) ---

class TestFetchNikkeiChange:
    @patch("src.data.jquants._fetch_nikkei_yfinance")
    def test_returns_float(self, mock_yf):
        mock_yf.return_value = 0.26
        result = fetch_nikkei_change()
        assert isinstance(result, float)
        assert 0.2 < result < 0.3

    @patch("src.data.jquants._fetch_nikkei_yfinance")
    def test_negative_change(self, mock_yf):
        mock_yf.return_value = -2.5
        result = fetch_nikkei_change()
        assert result < 0

    @patch("src.data.jquants._jquants_enabled", return_value=True)
    @patch("src.data.jquants._fetch_nikkei_jquants")
    def test_jquants_fallback(self, mock_jq, mock_enabled):
        mock_jq.side_effect = RuntimeError("expired")
        with patch("src.data.jquants._fetch_nikkei_yfinance") as mock_yf:
            mock_yf.return_value = -0.5
            result = fetch_nikkei_change()
            assert result == -0.5
            mock_yf.assert_called_once()


# --- fetch_all_tickers (Mocked) ---

class TestFetchAllTickers:
    @patch("src.data.jquants._fetch_ohlcv_yfinance")
    def test_returns_all_13(self, mock_yf):
        mock_yf.return_value = _make_ohlcv_df(30)
        results = fetch_all_tickers()
        assert len(results) == 13
        for ticker in TICKERS:
            assert ticker in results
            assert len(results[ticker]) >= 25


# --- JQUANTS_ENABLED切り替え ---

class TestEnvSwitch:
    @patch("src.data.jquants._fetch_ohlcv_yfinance")
    @patch("src.data.jquants._fetch_ohlcv_jquants")
    def test_disabled_uses_yfinance(self, mock_jq, mock_yf):
        mock_yf.return_value = _make_ohlcv_df(30)
        with patch.dict(os.environ, {"JQUANTS_ENABLED": "false"}):
            fetch_daily_ohlcv("4519")
            mock_yf.assert_called_once()
            mock_jq.assert_not_called()

    @patch("src.data.jquants._fetch_ohlcv_yfinance")
    @patch("src.data.jquants._fetch_ohlcv_jquants")
    def test_enabled_tries_jquants_first(self, mock_jq, mock_yf):
        mock_jq.return_value = _make_ohlcv_df(30)
        with patch.dict(os.environ, {"JQUANTS_ENABLED": "true"}):
            fetch_daily_ohlcv("4519")
            mock_jq.assert_called_once()
            mock_yf.assert_not_called()


# --- Integration (実API — yfinance) ---

@pytest.mark.integration
class TestIntegrationYfinance:
    """yfinanceで実データ取得テスト。"""

    def test_fetch_single_ticker(self):
        df = fetch_daily_ohlcv("4519")
        assert len(df) >= 25
        assert not df["close"].isna().any()

    def test_ma25_deviation_real(self):
        df = fetch_daily_ohlcv("4519")
        dev = calc_ma25_deviation(df)
        assert isinstance(dev, float)
        assert not math.isnan(dev)
        assert -1.0 < dev < 1.0

    def test_nikkei_change_real(self):
        change = fetch_nikkei_change()
        assert isinstance(change, float)
        assert -20.0 < change < 20.0

    def test_all_13_tickers(self):
        results = fetch_all_tickers()
        assert len(results) == 13
        for ticker, df in results.items():
            dev = calc_ma25_deviation(df)
            assert not math.isnan(dev), f"{ticker} has NaN deviation"
