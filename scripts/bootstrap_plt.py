"""Bootstrap PLT (Pattern Lookup Table) from existing closed positions.

Phase 0+ (2026-05-21):
Joins positions (closed) with scans on (ticker, entry_date) to recover
feature values at entry time, then aggregates per-cell statistics.

Limitations:
- scans table records {consensus, p1-p5, ma25_deviation}; RSI/BB are NOT
  saved. Phase 0 bootstrap uses placeholder bins (rsi_bin=1, bb_bin=0).
- Phase 1: extend scans schema to persist RSI / BB lower band, then re-bootstrap.

Usage:
    .venv/bin/python scripts/bootstrap_plt.py [--db data/grove_stock.duckdb] [--dry-run]
"""
from __future__ import annotations

import argparse
import sys
from collections import defaultdict
from pathlib import Path

import duckdb

# Allow running as script with repo root in PYTHONPATH
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.sizing.plt import (  # noqa: E402
    CellKey,
    aggregate_cell,
    assign_dev_bin,
    assign_regime,
    assign_sector,
    ensure_plt_table,
    upsert_cell,
)


# Phase 0 placeholders (Phase 1 will extract real values from scans-v2 schema)
PLACEHOLDER_RSI_BIN = 1  # [15, 25)
PLACEHOLDER_BB_BIN = 0   # [0, 0.3)


def map_dev_to_score(actual_dev: float | None, threshold_assumed: float = -0.07) -> float:
    """Approximate F2 deviation_depth from raw ma25_deviation.

    Bootstrap fallback: assumes typical sector threshold of -7% if not available.
    """
    if actual_dev is None or actual_dev >= 0:
        return 0.0
    ratio = abs(actual_dev) / abs(threshold_assumed)
    return min(ratio, 2.0) / 2.0


def bootstrap(
    db_path: str,
    *,
    dry_run: bool = False,
    verbose: bool = True,
) -> dict[str, int]:
    """Build initial plt_cells table from closed positions + scans.

    Returns:
        dict: {"trades_joined": N, "cells_populated": M, "cells_cold": K}
    """
    con = duckdb.connect(db_path, read_only=dry_run)
    if not dry_run:
        ensure_plt_table(con)

    # Pull closed positions + most recent scan for that ticker on entry_date.
    # ROW_NUMBER picks latest scan to handle duplicates.
    rows = con.execute(
        """
        WITH ranked AS (
            SELECT p.ticker, p.entry_date, p.exit_price, p.entry_price,
                   (p.exit_price - p.entry_price)/p.entry_price AS pnl_pct,
                   s.consensus, s.ma25_deviation,
                   ROW_NUMBER() OVER (
                       PARTITION BY p.ticker, p.entry_date
                       ORDER BY s.scanned_at DESC
                   ) AS rn
            FROM positions p
            LEFT JOIN scans s
              ON s.ticker = p.ticker
             AND CAST(s.scanned_at AS DATE) = p.entry_date
            WHERE p.status = 'closed'
              AND p.exit_price IS NOT NULL
              AND p.entry_price > 0
        )
        SELECT ticker, entry_date, pnl_pct, consensus, ma25_deviation
        FROM ranked
        WHERE rn = 1
        ORDER BY entry_date
        """,
    ).fetchall()

    if verbose:
        print(f"[bootstrap] joined {len(rows)} closed trades with scans")

    # Group pnl_pct by cell_id
    cell_pnls: dict[str, list[float]] = defaultdict(list)

    for ticker, entry_date, pnl_pct, consensus, ma25_dev in rows:
        if consensus is None or consensus < 3:
            # Skip trades that don't meet consensus minimum (likely legacy)
            continue
        dev_score = map_dev_to_score(ma25_dev)
        cell_key = CellKey(
            consensus=max(3, min(5, int(consensus))),
            dev_bin=assign_dev_bin(dev_score),
            rsi_bin=PLACEHOLDER_RSI_BIN,
            bb_bin=PLACEHOLDER_BB_BIN,
            regime="bear",  # bootstrap: assume bear (BNF逆張りは bear で発火しやすい)
            sector=assign_sector(ticker),
        )
        cell_pnls[cell_key.to_id()].append(float(pnl_pct))

    if verbose:
        print(f"[bootstrap] populated {len(cell_pnls)} unique cells")

    cold_count = 0
    warm_count = 0
    hot_count = 0
    for cell_id, pnls in cell_pnls.items():
        stats = aggregate_cell(cell_id, pnls)
        if verbose:
            print(
                f"  {cell_id}  n={stats.n_samples:2d}  wins={stats.n_wins:2d}  "
                f"avg_pnl={stats.avg_pnl_pct:+.3f}  shrunk_wr={stats.shrunk_win_rate:.3f}  "
                f"kelly={stats.kelly_fraction:.3f}  cap={stats.recommended_cap_pct:.3f}  "
                f"[{stats.confidence}]"
            )
        if stats.confidence == "cold":
            cold_count += 1
        elif stats.confidence == "warm":
            warm_count += 1
        else:
            hot_count += 1

        if not dry_run:
            upsert_cell(con, stats)

    summary = {
        "trades_joined": len(rows),
        "cells_populated": len(cell_pnls),
        "cells_cold": cold_count,
        "cells_warm": warm_count,
        "cells_hot": hot_count,
    }
    if verbose:
        print(f"\n[bootstrap] summary: {summary}")
    con.close()
    return summary


def main() -> int:
    parser = argparse.ArgumentParser(description="Bootstrap PLT from closed positions.")
    parser.add_argument("--db", default="data/grove_stock.duckdb", help="DuckDB path")
    parser.add_argument("--dry-run", action="store_true", help="No DB writes")
    parser.add_argument("--quiet", action="store_true")
    args = parser.parse_args()

    bootstrap(args.db, dry_run=args.dry_run, verbose=not args.quiet)
    return 0


if __name__ == "__main__":
    sys.exit(main())
