"""daily_quotes 過去 bar mutation 監査。

snapshot parquet (data/snapshots/daily_quotes_YYYYMMDD_*.parquet) と現在 cache
(data/history_cache.duckdb) を JOIN し、(code, date) ペアで close 値の不一致を検出する。

G3 baseline test (test_c3_baseline_frozen_g3) が 901 vs 555 で drift する原因の
仮説2「過去 bar が後日 update されている」を確証/deny するツール。

usage:
    .venv/bin/python scripts/audit_daily_quotes_mutation.py \
        --snapshot data/snapshots/daily_quotes_20260520_le_baseline.parquet
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import duckdb


def audit(snapshot_path: Path, cache_db: Path, tol_pct: float = 1e-6) -> dict:
    """snapshot と cache の (code, date, close) を比較。

    Returns: 統計 dict（snapshot rows, matched rows, mismatched rows, sample diffs）
    """
    con = duckdb.connect()
    con.execute(f"CREATE VIEW snap AS SELECT code, date, close FROM read_parquet('{snapshot_path}')")
    con.execute(f"ATTACH '{cache_db}' AS hist (READ_ONLY)")

    n_snap = con.execute("SELECT count(*) FROM snap").fetchone()[0]
    n_cache_overlap = con.execute(
        "SELECT count(*) FROM hist.daily_quotes h "
        "JOIN snap s USING (code, date)"
    ).fetchone()[0]

    # mismatch detection
    mismatch_rows = con.execute(f"""
        SELECT s.code, s.date, s.close AS snap_close, h.close AS cache_close,
               h.close - s.close AS diff,
               (h.close - s.close) / NULLIF(s.close, 0) AS diff_pct
        FROM snap s
        JOIN hist.daily_quotes h USING (code, date)
        WHERE abs((h.close - s.close) / NULLIF(s.close, 0)) > {tol_pct}
        ORDER BY abs(diff_pct) DESC
    """).fetchdf()

    # snapshot only (cache から消えた = code/date が deleted)
    n_deleted = con.execute(
        "SELECT count(*) FROM snap s "
        "LEFT JOIN hist.daily_quotes h USING (code, date) "
        "WHERE h.code IS NULL"
    ).fetchone()[0]

    con.close()

    return {
        "snapshot_rows": n_snap,
        "cache_overlap_rows": n_cache_overlap,
        "deleted_rows": n_deleted,  # snapshot にあって cache に無い
        "mutated_rows": len(mismatch_rows),
        "mismatch_sample": mismatch_rows.head(20),
    }


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--snapshot", required=True, type=Path,
                   help="snapshot parquet path")
    p.add_argument("--cache-db", type=Path,
                   default=Path("/Users/ryu/grove-stock/data/history_cache.duckdb"))
    p.add_argument("--tol-pct", type=float, default=1e-6,
                   help="relative tolerance for close value diff (default: 1e-6 = effectively exact)")
    args = p.parse_args()

    if not args.snapshot.exists():
        print(f"snapshot not found: {args.snapshot}", file=sys.stderr)
        return 1

    print(f"snapshot: {args.snapshot}")
    print(f"cache:    {args.cache_db}")
    print(f"tol_pct:  {args.tol_pct}")
    print()

    r = audit(args.snapshot, args.cache_db, tol_pct=args.tol_pct)
    print(f"snapshot rows:        {r['snapshot_rows']:,}")
    print(f"cache overlap rows:   {r['cache_overlap_rows']:,}")
    print(f"deleted from cache:   {r['deleted_rows']:,} (snapshot にあって cache に無い)")
    print(f"mutated rows:         {r['mutated_rows']:,} (close 値が変わった)")
    print()
    if r["mutated_rows"] > 0:
        print("=== 上位 20 件の mutation (|diff_pct| 降順) ===")
        print(r["mismatch_sample"].to_string(index=False))
        print()
        print("→ G3 仮説2 (daily_quotes mutation) を **CONFIRM**")
    else:
        print("→ G3 仮説2 (daily_quotes mutation) を **DENY**: 過去 close は不変")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
