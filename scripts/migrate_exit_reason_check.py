"""positions.exit_reason CHECK constraint を令和式 (gap_down_stop/trailing_stop追加) に migrate.

DuckDB は ALTER TABLE DROP CONSTRAINT 未サポートのため、table rebuild dance:
1. 新 schema で positions_new 作成
2. データ全コピー
3. 旧 positions DROP
4. positions_new を positions に RENAME

冪等: 既に新 CHECK ならスキップ。
"""
from __future__ import annotations

import sys
from pathlib import Path

# プロジェクトroot追加
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import duckdb

DB_PATH = "data/grove_stock.duckdb"


def main() -> None:
    con = duckdb.connect(DB_PATH)

    # 既に新 CHECK ならskip
    rows = con.execute(
        "SELECT constraint_text FROM duckdb_constraints() "
        "WHERE table_name='positions' AND constraint_text LIKE '%gap_down_stop%'"
    ).fetchall()
    if rows:
        print("[skip] CHECK constraint already includes gap_down_stop")
        return

    print("[migrate] positions.exit_reason CHECK を rebuild...")
    con.execute("BEGIN TRANSACTION;")
    try:
        # 1. 新 schema (db.py の最新 CHECK と同じ)
        con.execute("""
            CREATE TABLE positions_new (
                id INTEGER PRIMARY KEY DEFAULT nextval('positions_id_seq'),
                ticker TEXT NOT NULL,
                entry_price REAL NOT NULL,
                shares INTEGER NOT NULL,
                entry_date DATE NOT NULL,
                status TEXT NOT NULL CHECK (status IN ('open', 'closed')),
                exit_price REAL,
                exit_reason TEXT CHECK (
                    exit_reason IS NULL
                    OR exit_reason IN ('stop_loss', 'max_hold', 'take_profit', 'signal_reversal',
                                       'news_negative', 'gap_down_stop', 'trailing_stop')
                ),
                pnl REAL,
                closed_at TIMESTAMP,
                commission REAL DEFAULT 0.0,
                book TEXT DEFAULT 'legacy',
                max_price_seen REAL
            );
        """)

        # 2. データコピー (NULL OK な新 column max_price_seen も含む)
        n = con.execute("SELECT COUNT(*) FROM positions").fetchone()[0]
        con.execute("""
            INSERT INTO positions_new
            SELECT id, ticker, entry_price, shares, entry_date, status,
                   exit_price, exit_reason, pnl, closed_at, commission, book, max_price_seen
            FROM positions;
        """)
        copied = con.execute("SELECT COUNT(*) FROM positions_new").fetchone()[0]
        assert copied == n, f"copy mismatch {copied} != {n}"
        print(f"  copied {copied} rows")

        # 3. 旧 drop + rename
        con.execute("DROP TABLE positions;")
        con.execute("ALTER TABLE positions_new RENAME TO positions;")

        # 4. CHECK 確認
        new_check = con.execute(
            "SELECT constraint_text FROM duckdb_constraints() "
            "WHERE table_name='positions' AND constraint_text LIKE '%gap_down_stop%'"
        ).fetchall()
        assert new_check, "new CHECK not in place after rebuild"

        con.execute("COMMIT;")
        print(f"[done] migration完了, {copied} positions 維持")
    except Exception as e:
        con.execute("ROLLBACK;")
        print(f"[error] rollback: {e}")
        raise
    finally:
        con.close()


if __name__ == "__main__":
    main()
