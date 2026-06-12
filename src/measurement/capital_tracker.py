"""capital_tracker (plan S5 §2.1): ブック毎の拘束/空き枠を時系列スナップショット。

agent_architecture_plan §資金政策: 回収待ちは「拘束資金」、空き枠ぶんだけ
新規建玉。本モジュールは各ブックの equity/committed/free_cash を日次1行記録し
週次 council が「枠を踏まえた GO/PASS」判断に read-only 参照する。

冪等: 同一 snapshot_date の行は DELETE→INSERT で置換（同日再実行で増えない）。
live hot path に足さない（daily_update 後のバッチで snapshot() を呼ぶ想定）。
"""
from __future__ import annotations

import logging
from datetime import date

from config.books import BOOKS
from src.data.db import book_account, get_connection

logger = logging.getLogger("measurement.capital_tracker")

_DDL = """
CREATE TABLE IF NOT EXISTS capital_state (
    snapshot_date DATE NOT NULL,
    book          VARCHAR NOT NULL,
    equity        DOUBLE NOT NULL,
    committed     DOUBLE NOT NULL,
    free_cash     DOUBLE NOT NULL,
    open_count    INTEGER NOT NULL,
    max_concurrent INTEGER NOT NULL,
    slots_left    INTEGER NOT NULL,
    snapshot_at   TIMESTAMP DEFAULT now()
)
"""


def snapshot(db_path: str | None = None, on: date | None = None) -> list[dict]:
    """全ブックの資金状態を capital_state に1行ずつ記録（同日冪等）。

    Returns: 記録した行 dict のリスト（呼び出し側のログ/検証用）。
    """
    from src.main import MAX_CONCURRENT_POSITIONS  # 単一ソース由来（DEFAULTS）

    snap_date = on or date.today()
    # get_connection 経由で _create_tables（positions.book の冪等ALTER含む）を
    # 必ず通す＝book列が無い旧DBを自己修復（raw connectだとmigration素通り）。
    con = get_connection(db_path)
    rows: list[dict] = []
    try:
        con.execute(_DDL)
        con.execute("DELETE FROM capital_state WHERE snapshot_date = ?", [snap_date])
        for bk in BOOKS:
            equity, free_cash, open_tk = book_account(con, bk.book_id, bk.initial_capital)
            committed = equity - free_cash
            open_count = len(open_tk)
            slots_left = max(MAX_CONCURRENT_POSITIONS - open_count, 0)
            con.execute(
                "INSERT INTO capital_state "
                "(snapshot_date,book,equity,committed,free_cash,open_count,"
                "max_concurrent,slots_left) VALUES (?,?,?,?,?,?,?,?)",
                [snap_date, bk.book_id, equity, committed, free_cash,
                 open_count, MAX_CONCURRENT_POSITIONS, slots_left],
            )
            rows.append({
                "snapshot_date": snap_date, "book": bk.book_id,
                "equity": equity, "committed": committed,
                "free_cash": free_cash, "open_count": open_count,
                "max_concurrent": MAX_CONCURRENT_POSITIONS, "slots_left": slots_left,
            })
    finally:
        con.close()
    logger.info("capital snapshot %s: %d books", snap_date, len(rows))
    return rows
