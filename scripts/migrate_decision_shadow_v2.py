"""Migrate decision_shadow to v2 schema (Phase 0+ EVS/PLT).

Adds the following columns if they don't exist:
- evs_total           DOUBLE
- cell_id             VARCHAR
- cap_pct_recommended DOUBLE
- cap_pct_actual      DOUBLE
- sizing_source       VARCHAR
- cell_n_samples      INTEGER
- cell_confidence     VARCHAR
- exploration_flag    BOOLEAN
- evs_components_json TEXT

Idempotent: checks via information_schema before ALTERing.

Usage:
    .venv/bin/python scripts/migrate_decision_shadow_v2.py [--db data/grove_stock.duckdb] [--dry-run]
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import duckdb

NEW_COLUMNS = [
    ("evs_total", "DOUBLE"),
    ("cell_id", "VARCHAR"),
    ("cap_pct_recommended", "DOUBLE"),
    ("cap_pct_actual", "DOUBLE"),
    ("sizing_source", "VARCHAR"),
    ("cell_n_samples", "INTEGER"),
    ("cell_confidence", "VARCHAR"),
    ("exploration_flag", "BOOLEAN"),
    ("evs_components_json", "TEXT"),
]


def current_columns(con: duckdb.DuckDBPyConnection) -> set[str]:
    rows = con.execute(
        """
        SELECT column_name FROM information_schema.columns
        WHERE table_name = 'decision_shadow'
        """
    ).fetchall()
    return {r[0] for r in rows}


def migrate(db_path: str, *, dry_run: bool = False, verbose: bool = True) -> int:
    con = duckdb.connect(db_path, read_only=dry_run)
    try:
        existing = current_columns(con)
        if verbose:
            print(f"[migrate] existing columns: {sorted(existing)}")

        added = 0
        for name, col_type in NEW_COLUMNS:
            if name in existing:
                if verbose:
                    print(f"[migrate]   skip {name}: already exists")
                continue
            stmt = f"ALTER TABLE decision_shadow ADD COLUMN {name} {col_type}"
            if dry_run:
                if verbose:
                    print(f"[migrate]   DRY-RUN: {stmt}")
            else:
                con.execute(stmt)
                if verbose:
                    print(f"[migrate]   added: {name} {col_type}")
                added += 1

        if verbose:
            print(f"[migrate] done. {added} columns added.")
        return added
    finally:
        con.close()


def main() -> int:
    parser = argparse.ArgumentParser(description="decision_shadow v2 migration.")
    parser.add_argument("--db", default="data/grove_stock.duckdb")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--quiet", action="store_true")
    args = parser.parse_args()
    migrate(args.db, dry_run=args.dry_run, verbose=not args.quiet)
    return 0


if __name__ == "__main__":
    sys.exit(main())
