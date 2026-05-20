"""J-Quants履歴データのローカルDuckDBキャッシュ。
API rate limit回避 + バックテスト高速化。
"""
from __future__ import annotations

import logging
import os
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

import pandas as pd
from dotenv import load_dotenv

from src.data.db import connect_duckdb

logger = logging.getLogger("data.cache")

CACHE_DIR = Path(__file__).resolve().parent.parent.parent / "data"
CACHE_DB = CACHE_DIR / "history_cache.duckdb"

# スロットル: 3 req/sec → 333ms 間隔
THROTTLE_SEC = float(os.getenv("JQUANTS_THROTTLE_SEC", "0.35"))


def _get_client():
    import jquantsapi
    load_dotenv(CACHE_DIR.parent / ".env")
    key = (os.getenv("JQUANTS_API_KEY") or os.getenv("JQUANTS_REFRESH_TOKEN") or "").strip()
    if not key:
        raise RuntimeError("JQUANTS_REFRESH_TOKEN not set")
    return jquantsapi.ClientV2(api_key=key)


def init_cache():
    CACHE_DIR.mkdir(exist_ok=True)
    con = connect_duckdb(CACHE_DB)
    con.execute("""
        CREATE TABLE IF NOT EXISTS daily_quotes (
            code VARCHAR NOT NULL,
            date DATE NOT NULL,
            open DOUBLE, high DOUBLE, low DOUBLE, close DOUBLE,
            volume DOUBLE, value DOUBLE,
            PRIMARY KEY (code, date)
        )
    """)
    con.execute("CREATE INDEX IF NOT EXISTS idx_daily_code ON daily_quotes(code)")
    con.execute("CREATE INDEX IF NOT EXISTS idx_daily_date ON daily_quotes(date)")
    con.execute("""
        CREATE TABLE IF NOT EXISTS universe_snapshot (
            code VARCHAR PRIMARY KEY,
            ticker VARCHAR, co_name VARCHAR,
            s33 VARCHAR, s33_name VARCHAR, scale_cat VARCHAR,
            updated_at TIMESTAMP DEFAULT now()
        )
    """)
    # Nikkei 225 close 永続 cache。yfinance moving window による drift から
    # backtest の reproducibility を保護する（characterization test の G3 baseline 安定化）。
    con.execute("""
        CREATE TABLE IF NOT EXISTS nikkei_bars (
            date DATE PRIMARY KEY,
            close DOUBLE NOT NULL
        )
    """)
    con.close()


def fetch_and_cache_nikkei(years: int = 5) -> int:
    """Nikkei 225 を yfinance から取得して nikkei_bars に upsert。冪等。Returns: 書き込み行数。"""
    import yfinance as yf

    init_cache()
    df = yf.Ticker("^N225").history(period=f"{years}y")
    if df is None or df.empty:
        return 0
    df = df.reset_index()[["Date", "Close"]].rename(columns={"Date": "date", "Close": "close"})
    df["date"] = pd.to_datetime(df["date"]).dt.tz_localize(None).dt.date
    df = df.dropna(subset=["close"])
    con = connect_duckdb(CACHE_DB)
    try:
        con.execute("INSERT OR REPLACE INTO nikkei_bars (date, close) SELECT date, close FROM df")
        return len(df)
    finally:
        con.close()


def load_nikkei_bars(end_date: Optional[str] = None) -> pd.DataFrame:
    """nikkei_bars から close を取得。end_date 指定時はその日以前で切る。

    cache が空なら空 DataFrame を返す（呼び出し側で fallback 判断する）。
    """
    con = connect_duckdb(CACHE_DB, read_only=True)
    sql = "SELECT date, close FROM nikkei_bars"
    params: list = []
    if end_date:
        sql += " WHERE date <= ?"
        params.append(end_date)
    sql += " ORDER BY date"
    try:
        df = con.execute(sql, params).fetchdf()
    finally:
        con.close()
    return df


def save_universe(universe: pd.DataFrame):
    init_cache()
    con = connect_duckdb(CACHE_DB)
    con.execute("DELETE FROM universe_snapshot")
    for _, r in universe.iterrows():
        con.execute("INSERT INTO universe_snapshot (code, ticker, co_name, s33, s33_name, scale_cat) VALUES (?,?,?,?,?,?)",
                    [r["Code"], r.get("ticker", r["Code"][:4]), r["CoName"], r["S33"], r["S33Nm"], r["ScaleCat"]])
    con.close()


def load_universe() -> pd.DataFrame:
    con = connect_duckdb(CACHE_DB, read_only=True)
    df = con.execute("SELECT code AS Code, ticker, co_name AS CoName, s33 AS S33, s33_name AS S33Nm, scale_cat AS ScaleCat FROM universe_snapshot").fetchdf()
    con.close()
    return df


_CLIENT_SINGLETON = None

def _shared_client():
    global _CLIENT_SINGLETON
    if _CLIENT_SINGLETON is None:
        _CLIENT_SINGLETON = _get_client()
    return _CLIENT_SINGLETON


def fetch_and_cache(code: str, years: int = 3, force: bool = False, client=None) -> int:
    """1銘柄の履歴を取得してキャッシュに投入。Returns rows added."""
    init_cache()
    con = connect_duckdb(CACHE_DB)
    existing = con.execute("SELECT MAX(date) FROM daily_quotes WHERE code=?", [code]).fetchone()[0]
    end = datetime.now()
    if existing and not force:
        if (end.date() - existing).days < 2:
            con.close()
            return 0
        start = datetime.combine(existing, datetime.min.time()) + timedelta(days=1)
    else:
        start = end - timedelta(days=int(years*365.25))

    client = client or _shared_client()
    try:
        df = client.get_eq_bars_daily(code=code, from_yyyymmdd=start.strftime("%Y%m%d"), to_yyyymmdd=end.strftime("%Y%m%d"))
    except Exception as e:
        con.close()
        raise
    time.sleep(THROTTLE_SEC)

    if df is None or df.empty:
        con.close()
        return 0
    df = df.rename(columns={"Date":"date","AdjO":"open","AdjH":"high","AdjL":"low","AdjC":"close","AdjVo":"volume","Va":"value"})
    df = df[["date","open","high","low","close","volume","value"]].copy().dropna(subset=["close"])
    df["code"] = code
    df["date"] = pd.to_datetime(df["date"]).dt.date

    # bulk insert with ignore-on-conflict
    con.register("_tmp", df)
    con.execute("INSERT OR IGNORE INTO daily_quotes SELECT code,date,open,high,low,close,volume,value FROM _tmp")
    n = con.execute("SELECT changes()").fetchone()[0] if False else len(df)
    con.close()
    return n


def fetch_and_cache_yfinance(code: str, years: int = 3) -> int:
    """yfinance fallback. code は 5桁 (例 '72030') で渡すが、yfinanceは4桁.T (例 '7203.T')。"""
    import yfinance as yf
    ticker = code[:4] + ".T"
    end = datetime.now()
    start = end - timedelta(days=int(years*365.25))
    t = yf.Ticker(ticker)
    df = t.history(start=start.strftime("%Y-%m-%d"), end=end.strftime("%Y-%m-%d"))
    if df is None or df.empty:
        return 0
    df = df.reset_index().rename(columns={"Date":"date","Open":"open","High":"high","Low":"low","Close":"close","Volume":"volume"})
    df["date"] = pd.to_datetime(df["date"]).dt.tz_localize(None).dt.date
    df["value"] = df["close"] * df["volume"]
    df["code"] = code
    df = df[["code","date","open","high","low","close","volume","value"]].dropna(subset=["close"])
    con = connect_duckdb(CACHE_DB)
    con.register("_tmp", df)
    con.execute("INSERT OR IGNORE INTO daily_quotes SELECT code,date,open,high,low,close,volume,value FROM _tmp")
    con.close()
    return len(df)


def bulk_fetch_yfinance(codes: list[str], years: int = 3, progress_every: int = 50) -> dict:
    """yfinanceで残銘柄を埋める。rate limitなし、ただし1秒ほどレスポンス遅い。"""
    stats = {"ok": 0, "err": 0, "err_codes": []}
    for i, code in enumerate(codes):
        try:
            n = fetch_and_cache_yfinance(code, years=years)
            if n > 0:
                stats["ok"] += 1
            else:
                stats["err"] += 1
                stats["err_codes"].append((code, "empty"))
        except Exception as e:
            stats["err"] += 1
            stats["err_codes"].append((code, str(e)[:80]))
        if (i+1) % progress_every == 0:
            logger.info("yf progress: %d/%d ok=%d err=%d", i+1, len(codes), stats["ok"], stats["err"])
    return stats


def bulk_fetch(codes: list[str], years: int = 3, progress_every: int = 20, max_retries: int = 3) -> dict:
    """複数コードをrate limit考慮してキャッシュ。成功/失敗カウントを返す。
    429時: exponential backoff (2s, 4s, 8s)してリトライ。"""
    stats = {"ok": 0, "err": 0, "err_codes": [], "retries": 0}
    client = _shared_client()
    for i, code in enumerate(codes):
        last_err = None
        for attempt in range(max_retries):
            try:
                fetch_and_cache(code, years=years, client=client)
                stats["ok"] += 1
                last_err = None
                break
            except Exception as e:
                last_err = str(e)[:100]
                if "429" in last_err or "too many" in last_err.lower():
                    wait = 2.0 * (2 ** attempt)
                    logger.warning("429 on %s attempt=%d, sleeping %.1fs", code, attempt+1, wait)
                    time.sleep(wait)
                    stats["retries"] += 1
                    continue
                break
        if last_err is not None:
            stats["err"] += 1
            stats["err_codes"].append((code, last_err))
        if (i+1) % progress_every == 0:
            logger.info("cache progress: %d/%d ok=%d err=%d retries=%d", i+1, len(codes), stats["ok"], stats["err"], stats["retries"])
    return stats


def load_bars(code: str, start: Optional[str] = None, end: Optional[str] = None) -> pd.DataFrame:
    """キャッシュから日足を取得。"""
    con = connect_duckdb(CACHE_DB, read_only=True)
    sql = "SELECT date,open,high,low,close,volume,value FROM daily_quotes WHERE code=?"
    params = [code]
    if start: sql += " AND date >= ?"; params.append(start)
    if end: sql += " AND date <= ?"; params.append(end)
    sql += " ORDER BY date"
    df = con.execute(sql, params).fetchdf()
    con.close()
    return df


def cache_stats() -> dict:
    con = connect_duckdb(CACHE_DB, read_only=True)
    n_codes = con.execute("SELECT COUNT(DISTINCT code) FROM daily_quotes").fetchone()[0]
    n_rows = con.execute("SELECT COUNT(*) FROM daily_quotes").fetchone()[0]
    date_range = con.execute("SELECT MIN(date), MAX(date) FROM daily_quotes").fetchone()
    con.close()
    return {"codes": n_codes, "rows": n_rows, "date_min": str(date_range[0]), "date_max": str(date_range[1])}
