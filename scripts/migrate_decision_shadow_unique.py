"""decision_shadow に UNIQUE(ticker, book, decided_date) 制約を追加する1回限りの migration。

2026-05-20 commit で record_proposal を ON CONFLICT DO UPDATE 化したが、既存 DB は
重複行 + UNIQUE 制約なしの状態。これを dedupe + 制約追加で揃える。

冪等: 既に UNIQUE 制約があれば no-op。複数回実行可能。

usage:
    .venv/bin/python scripts/migrate_decision_shadow_unique.py
"""
from __future__ import annotations

import logging
from pathlib import Path

import duckdb

from src.data.db import DEFAULT_DB_PATH

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("migrate.decision_shadow_unique")


def migrate(db_path: str | Path = DEFAULT_DB_PATH) -> dict:
    """重複削除 + UNIQUE 制約追加。

    Returns: 統計 dict (dedup_removed, unique_added)
    """
    db_path = str(db_path)
    con = duckdb.connect(db_path)
    stats = {"dedup_removed": 0, "unique_added": False, "skipped": False}
    try:
        # 既に UNIQUE 制約があるかを確認
        constraints = con.execute("""
            SELECT constraint_text FROM duckdb_constraints()
            WHERE table_name = 'decision_shadow'
              AND constraint_type = 'UNIQUE'
        """).fetchdf()
        already_unique = any(
            "ticker" in c and "book" in c and "decided_date" in c
            for c in constraints["constraint_text"].astype(str)
        )
        if already_unique:
            logger.info("UNIQUE constraint already exists — skipping migration")
            stats["skipped"] = True
            return stats

        # Step 1: 重複削除（最新 id を残す）
        before = con.execute("SELECT count(*) FROM decision_shadow").fetchone()[0]
        con.execute("""
            DELETE FROM decision_shadow
            WHERE id NOT IN (
                SELECT max(id) FROM decision_shadow
                GROUP BY ticker, book, decided_date
            )
        """)
        after = con.execute("SELECT count(*) FROM decision_shadow").fetchone()[0]
        stats["dedup_removed"] = before - after
        logger.info("dedup: %d → %d rows (removed %d duplicates)",
                    before, after, stats["dedup_removed"])

        # Step 2: UNIQUE 制約追加
        # DuckDB は ALTER TABLE ADD CONSTRAINT UNIQUE をサポートしないので新 table 作成
        # → SELECT INSERT → DROP 旧 → RENAME 新 のパターン
        # id seq を最大 id+1 に進めてから新 table を nextval DEFAULT で作る
        max_id = con.execute("SELECT COALESCE(max(id), 0) FROM decision_shadow").fetchone()[0]
        con.execute(f"CREATE OR REPLACE SEQUENCE decision_shadow_id_seq START {max_id + 1}")
        con.execute("""
            CREATE TABLE decision_shadow_new (
                id INTEGER DEFAULT nextval('decision_shadow_id_seq') PRIMARY KEY,
                decided_at    TIMESTAMP NOT NULL,
                decided_date  DATE NOT NULL,
                ticker        VARCHAR NOT NULL,
                book          VARCHAR,
                decision      VARCHAR NOT NULL CHECK (decision IN ('go','pass')),
                proposed_yen  DOUBLE,
                consensus     INTEGER,
                edge          DOUBLE,
                council_reason TEXT,
                cf_exit_date  DATE,
                cf_exit_reason VARCHAR,
                cf_pnl_pct    DOUBLE,
                cf_won        BOOLEAN,
                cf_computed_at TIMESTAMP,
                UNIQUE (ticker, book, decided_date)
            )
        """)
        con.execute("""
            INSERT INTO decision_shadow_new
            SELECT * FROM decision_shadow
        """)
        con.execute("DROP TABLE decision_shadow")
        con.execute("ALTER TABLE decision_shadow_new RENAME TO decision_shadow")
        stats["unique_added"] = True
        logger.info("UNIQUE constraint added. id seq reset to %d", max_id + 1)
    finally:
        con.close()
    return stats


if __name__ == "__main__":
    s = migrate()
    print(s)
