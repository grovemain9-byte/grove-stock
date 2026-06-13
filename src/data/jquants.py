"""データ取得 + MA25計算。

Issue #3: ingestion_nodeの核心処理。
13銘柄の日足OHLCV取得、MA25乖離率計算、日経225騰落率取得。

データソース:
  - JQUANTS_ENABLED=true → J-Quants API（優先）
  - JQUANTS_ENABLED=false or J-Quants失敗 → yfinance（ticker + ".T"）
"""
from __future__ import annotations

import logging
import os
from datetime import datetime, timedelta

import pandas as pd
import yfinance as yf

logger = logging.getLogger("data.jquants")

# 対象13銘柄
TICKERS = [
    "4519", "4568", "4523",   # 薬品: 中外製薬, 第一三共, エーザイ
    "7203", "7267", "7011",   # 自動車/重工: トヨタ, ホンダ, 三菱重工
    "8306", "8316",           # 金融: 三菱UFJ, 三井住友
    "9984",                   # IT: ソフトバンクG
    "6758", "6861",           # 電機: ソニー, キーエンス
    "9433",                   # 通信: KDDI
    "2914",                   # 食品: JT
]

# 日経225 yfinanceティッカー
NIKKEI225_YF = "^N225"


def _jquants_enabled() -> bool:
    return os.getenv("JQUANTS_ENABLED", "false").lower() in ("true", "1", "yes")


def _get_jquants_client():
    """J-Quants ClientV2を返す。importエラー or 認証情報なしならNone。

    優先: JQUANTS_API_KEY → JQUANTS_MAILADDRESS+JQUANTS_PASSWORD
    """
    try:
        import jquantsapi
    except ImportError:
        return None

    placeholder = {"", "your_email", "your_password", "your_api_key"}

    api_key = (os.getenv("JQUANTS_API_KEY") or os.getenv("JQUANTS_REFRESH_TOKEN") or "").strip()
    if api_key and api_key not in placeholder:
        return jquantsapi.ClientV2(api_key=api_key)

    mail = os.getenv("JQUANTS_MAILADDRESS", "").strip()
    password = os.getenv("JQUANTS_PASSWORD", "").strip()
    if mail and password and mail not in placeholder and password not in placeholder and "@" in mail:
        return jquantsapi.Client(mail_address=mail, password=password)

    return None


# === yfinance実装 ===

def _fetch_ohlcv_yfinance(ticker: str, days: int = 60) -> pd.DataFrame:
    """yfinanceで日足OHLCV取得。"""
    yf_ticker = f"{ticker}.T"
    end = datetime.now()
    start = end - timedelta(days=days + 10)

    t = yf.Ticker(yf_ticker)
    df = t.history(start=start.strftime("%Y-%m-%d"), end=end.strftime("%Y-%m-%d"))

    if df is None or df.empty:
        raise ValueError(f"No yfinance data for {yf_ticker}")

    df = df.reset_index()
    df = df.rename(columns={
        "Date": "date",
        "Open": "open",
        "High": "high",
        "Low": "low",
        "Close": "close",
        "Volume": "volume",
    })

    df = df[["date", "open", "high", "low", "close", "volume"]].copy()
    df["date"] = pd.to_datetime(df["date"]).dt.tz_localize(None)
    df = df.dropna(subset=["close"])
    df = df.sort_values("date").reset_index(drop=True)

    return df


def _fetch_nikkei_yfinance() -> float:
    """yfinanceで日経225騰落率を取得。"""
    t = yf.Ticker(NIKKEI225_YF)
    df = t.history(period="5d")

    if df is None or len(df) < 2:
        raise ValueError("Insufficient Nikkei 225 data from yfinance")

    closes = df["Close"].values
    prev, curr = closes[-2], closes[-1]

    if prev == 0:
        return 0.0

    return ((curr - prev) / prev) * 100


# === J-Quants実装 ===

def _fetch_ohlcv_jquants(ticker: str, days: int = 60) -> pd.DataFrame:
    """J-Quants APIで日足OHLCV取得。"""
    client = _get_jquants_client()
    if client is None:
        raise RuntimeError("J-Quants client unavailable")

    end = datetime.now()
    start = end - timedelta(days=days + 10)

    df = client.get_eq_bars_daily(
        code=ticker,
        from_yyyymmdd=start.strftime("%Y%m%d"),
        to_yyyymmdd=end.strftime("%Y%m%d"),
    )

    if df is None or df.empty:
        raise ValueError(f"No J-Quants data for {ticker}")

    # V2 column mapping: Date/O/H/L/C/Vo + Adj variants
    col_map = {"Date": "date", "AdjO": "open", "AdjH": "high", "AdjL": "low",
               "AdjC": "close", "AdjVo": "volume"}
    df = df.rename(columns=col_map)

    df = df[["date", "open", "high", "low", "close", "volume"]].copy()
    df = df.dropna(subset=["close"])
    df = df.sort_values("date").reset_index(drop=True)

    return df


def _fetch_nikkei_jquants() -> float:
    """J-Quants APIで日経225騰落率を取得。"""
    client = _get_jquants_client()
    if client is None:
        raise RuntimeError("J-Quants client unavailable")

    end = datetime.now()
    start = end - timedelta(days=10)

    df = client.get_idx_bars_daily(
        code="0000",
        from_yyyymmdd=start.strftime("%Y%m%d"),
        to_yyyymmdd=end.strftime("%Y%m%d"),
    )

    if df is None or df.empty:
        raise ValueError("No Nikkei 225 data from J-Quants")

    for col in df.columns:
        if "close" in col.lower():
            close_col = col
            break
    else:
        raise ValueError(f"No close column in: {list(df.columns)}")

    df = df.sort_values(df.columns[0]).reset_index(drop=True)
    closes = df[close_col].astype(float)

    if len(closes) < 2:
        raise ValueError("Need at least 2 days of Nikkei data")

    prev, curr = closes.iloc[-2], closes.iloc[-1]
    if prev == 0:
        return 0.0
    return ((curr - prev) / prev) * 100


# === 公開API（J-Quants優先、yfinanceフォールバック） ===

def fetch_daily_ohlcv(ticker: str, days: int = 60) -> pd.DataFrame:
    """日足OHLCVを取得。

    JQUANTS_ENABLED=true → J-Quants優先、失敗時yfinanceフォールバック。
    JQUANTS_ENABLED=false → yfinance直接。

    Returns:
        DataFrame with columns: date, open, high, low, close, volume
    """
    source = "yfinance"

    if _jquants_enabled():
        try:
            df = _fetch_ohlcv_jquants(ticker, days)
            source = "jquants"
            logger.info("Fetched %s from J-Quants: %d rows", ticker, len(df))
        except Exception as e:
            logger.warning("J-Quants failed for %s: %s — falling back to yfinance", ticker, e)
            df = _fetch_ohlcv_yfinance(ticker, days)
    else:
        df = _fetch_ohlcv_yfinance(ticker, days)

    if len(df) < 25:
        raise ValueError(f"Insufficient data for {ticker}: {len(df)} rows (need >=25), source={source}")

    return df


def calc_ma25_deviation(df: pd.DataFrame) -> float:
    """MA25乖離率を計算。(直近終値 - MA25) / MA25 を返す。

    Returns:
        乖離率（例: -0.05 = -5%）
    """
    if len(df) < 25:
        raise ValueError(f"Need >=25 rows, got {len(df)}")

    close = df["close"].astype(float)
    ma25 = close.rolling(25).mean().iloc[-1]

    if pd.isna(ma25) or ma25 == 0:
        raise ValueError("MA25 is NaN or zero")

    latest_close = close.iloc[-1]
    return (latest_close - ma25) / ma25


def fetch_nikkei_change() -> float:
    """日経225の当日騰落率（%）を返す。

    Returns:
        騰落率（例: -1.5 = -1.5%下落）
    """
    if _jquants_enabled():
        try:
            return _fetch_nikkei_jquants()
        except Exception as e:
            logger.warning("J-Quants Nikkei failed: %s — falling back to yfinance", e)
            return _fetch_nikkei_yfinance()
    else:
        return _fetch_nikkei_yfinance()


def fetch_nikkei_ma25_deviation() -> float:
    """日経225のMA25乖離率（decimal, 例: -0.03 = -3%）を返す。
    P4 regime filter用。J-Quants Lightはindex未対応のため yfinance で取得。
    """
    t = yf.Ticker(NIKKEI225_YF)
    df = t.history(period="60d")
    if df is None or df.empty:
        raise ValueError("No Nikkei 225 data")
    close = df["Close"].astype(float).dropna()
    if len(close) < 25:
        raise ValueError(f"Insufficient Nikkei data: {len(close)} rows")
    ma25 = close.rolling(25).mean().iloc[-1]
    if pd.isna(ma25) or ma25 == 0:
        raise ValueError("Nikkei MA25 NaN or zero")
    return float((close.iloc[-1] - ma25) / ma25)


def fetch_all_tickers(days: int = 60) -> dict[str, pd.DataFrame]:
    """全13銘柄の日足OHLCVを取得。

    Returns:
        {ticker: DataFrame} の辞書
    """
    results = {}
    for ticker in TICKERS:
        try:
            results[ticker] = fetch_daily_ohlcv(ticker, days)
        except Exception as e:
            logger.error("Failed to fetch %s: %s", ticker, e)
            raise
    return results
