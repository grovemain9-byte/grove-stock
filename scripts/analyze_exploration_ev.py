"""Exploration vs Exploitation EV analyzer.

ε-greedy router の exploration_flag 別に cf_pnl_pct を集計し、
「exploration trade のEV」と「exploitation trade のEV」を比較。

Phase 0+ (Grove 2026-05-21):
ε-greedy が「未経験パターンを試す」効果が測れるダッシュボード。
exploration EV > exploitation EV → 探索率を維持 or 上げる根拠
exploration EV < exploitation EV → 探索率を下げる根拠

Usage:
    .venv/bin/python scripts/analyze_exploration_ev.py [--db data/grove_stock.duckdb] [--days 30]
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import duckdb

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


REPORTS = {
    "sizing_source_distribution": """
        SELECT sizing_source,
               COUNT(*) AS n,
               COUNT(cf_pnl_pct) AS cf_resolved,
               ROUND(AVG(cf_pnl_pct) * 100, 3) AS avg_cf_pnl_pct,
               ROUND(STDDEV(cf_pnl_pct) * 100, 3) AS std_cf_pnl_pct,
               SUM(CASE WHEN cf_won THEN 1 ELSE 0 END) AS n_wins
        FROM decision_shadow
        WHERE sizing_source IS NOT NULL
          AND decided_date >= CURRENT_DATE - INTERVAL '{days}' DAY
        GROUP BY sizing_source
        ORDER BY n DESC
    """,
    "exploration_vs_exploitation": """
        SELECT
            CASE WHEN exploration_flag THEN 'exploration' ELSE 'exploitation' END AS mode,
            COUNT(*) AS n,
            COUNT(cf_pnl_pct) AS cf_resolved,
            ROUND(AVG(cf_pnl_pct) * 100, 3) AS avg_pnl_pct,
            SUM(CASE WHEN cf_won THEN 1 ELSE 0 END) AS wins,
            ROUND(100.0 * SUM(CASE WHEN cf_won THEN 1 ELSE 0 END) / NULLIF(COUNT(cf_pnl_pct), 0), 1) AS win_rate_pct
        FROM decision_shadow
        WHERE exploration_flag IS NOT NULL
          AND decided_date >= CURRENT_DATE - INTERVAL '{days}' DAY
        GROUP BY mode
        ORDER BY mode
    """,
    "cell_growth": """
        SELECT cell_id,
               n_samples,
               n_wins,
               ROUND(shrunk_win_rate * 100, 1) AS shrunk_wr_pct,
               ROUND(recommended_cap_pct * 100, 1) AS rec_cap_pct,
               confidence,
               last_updated
        FROM plt_cells
        WHERE n_samples >= 1
        ORDER BY n_samples DESC, cell_id
        LIMIT 30
    """,
    "evs_factor_correlation": """
        WITH resolved AS (
            SELECT cap_pct_actual, cf_pnl_pct, evs_total
            FROM decision_shadow
            WHERE cf_pnl_pct IS NOT NULL
              AND cap_pct_actual IS NOT NULL
              AND decided_date >= CURRENT_DATE - INTERVAL '{days}' DAY
        )
        SELECT
            COUNT(*) AS n,
            ROUND(CORR(evs_total, cf_pnl_pct), 4) AS corr_evs_pnl,
            ROUND(CORR(cap_pct_actual, cf_pnl_pct), 4) AS corr_cap_pnl,
            ROUND(AVG(cf_pnl_pct) * 100, 3) AS overall_avg_pnl_pct
        FROM resolved
    """,
    "council_reason_outcomes": """
        SELECT council_reason,
               COUNT(*) AS n,
               COUNT(cf_pnl_pct) AS cf_resolved,
               ROUND(AVG(cf_pnl_pct) * 100, 3) AS avg_cf_pnl_pct,
               SUM(CASE WHEN cf_won THEN 1 ELSE 0 END) AS wins
        FROM decision_shadow
        WHERE decided_date >= CURRENT_DATE - INTERVAL '{days}' DAY
        GROUP BY council_reason
        ORDER BY n DESC
    """,
}


def run(db_path: str, days: int, verbose: bool = True) -> None:
    con = duckdb.connect(db_path, read_only=True)
    try:
        for name, sql in REPORTS.items():
            print(f"\n=== {name} (last {days} days) ===")
            df = con.execute(sql.format(days=days)).fetchdf()
            if df.empty:
                print("  (no data)")
            else:
                print(df.to_string(index=False))
    finally:
        con.close()


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--db", default="data/grove_stock.duckdb")
    p.add_argument("--days", type=int, default=30)
    args = p.parse_args()
    run(args.db, args.days)
    return 0


if __name__ == "__main__":
    sys.exit(main())
