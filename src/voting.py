"""Voting Node: P1〜P5並列実行 → コンセンサス集計 → DB記録。

Issue #5: scan_graphの中核。
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime

import duckdb
import pandas as pd

from src.players import p01, p02, p03, p04, p05
from src.data.db import get_connection
from config.strategy_params import DEFAULTS

logger = logging.getLogger("voting")

# S3: 単一ソース DEFAULTS を参照（値は現値3と同一＝挙動保存）
CONSENSUS_THRESHOLD = DEFAULTS.consensus_min


def _write_scan(
    con: duckdb.DuckDBPyConnection,
    ticker: str,
    ma25_dev: float | None,
    nikkei_change_pct: float,
    votes: dict,
) -> None:
    """scans へ1行 INSERT（接続は呼び出し側が管理）。"""
    con.execute("""
        INSERT INTO scans (ticker, scanned_at, ma25_deviation, nikkei_change,
                           p1, p2, p3, p4, p5, consensus)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, [
        ticker, datetime.now(), ma25_dev, nikkei_change_pct,
        votes["p1"], votes["p2"], votes["p3"], votes["p4"], votes["p5"],
        votes["consensus"],
    ])


async def vote_all(
    ticker: str,
    df: pd.DataFrame,
    nikkei_change_pct: float,
    *,
    nikkei_ma25_dev: float | None = None,
    db_path: str | None = None,
    con: duckdb.DuckDBPyConnection | None = None,
) -> dict:
    """P1〜P5を並列実行し、コンセンサスを集計してDB記録。

    Args:
        ticker: 銘柄コード
        df: 日足OHLCVデータ
        nikkei_change_pct: 日経225当日騰落率（%）
        db_path: DuckDBパス（テスト用。Noneならデフォルト）
        con: 既存DuckDB接続。渡されたら open/close せず再利用＝494銘柄ループで
             接続+DDLを1回に削減（ロック競合の主因を除去）。未指定なら従来通り
             銘柄毎に get_connection（monitor/既存テスト挙動を完全維持）。

    Returns:
        {"p1": bool, "p2": bool, ..., "p5": bool, "consensus": int}
    """
    results = await asyncio.gather(
        p01.vote(df, ticker=ticker),
        p02.vote(df),
        p03.vote(df),
        p04.vote(df, nikkei_change_pct=nikkei_change_pct, nikkei_ma25_dev=nikkei_ma25_dev),
        p05.vote(df),
        return_exceptions=True,
    )

    # 例外はFalse（棄権）扱い
    results = [False if isinstance(r, Exception) else r for r in results]

    votes = {
        "p1": bool(results[0]),
        "p2": bool(results[1]),
        "p3": bool(results[2]),
        "p4": bool(results[3]),
        "p5": bool(results[4]),
    }
    votes["consensus"] = sum(votes.values())

    # MA25乖離率を計算（DB記録用 + kelly_node の decision_shadow 用）
    ma25_dev = None
    try:
        close = df["close"].astype(float)
        ma25 = close.rolling(25).mean().iloc[-1]
        if not pd.isna(ma25) and ma25 != 0:
            ma25_dev = float((close.iloc[-1] - ma25) / ma25)
    except Exception:
        pass
    votes["dev"] = ma25_dev

    # DB記録: con渡されたら共有接続を再利用、なければ従来通り銘柄毎に開閉
    try:
        if con is not None:
            _write_scan(con, ticker, ma25_dev, nikkei_change_pct, votes)
        else:
            c = get_connection(db_path)
            try:
                _write_scan(c, ticker, ma25_dev, nikkei_change_pct, votes)
            finally:
                c.close()
    except Exception as e:
        logger.error("DB write failed for %s: %s", ticker, e)

    return votes
