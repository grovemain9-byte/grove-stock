"""Phase 1-B: news_negative exit付きバックテスト + 従来版との比較レポート。
python -m src.backtest.runner_phase1b
"""
from __future__ import annotations

import argparse
import json
import logging
from datetime import date, datetime
from pathlib import Path

from src.backtest.runner import compute_sector_thresholds_from_cache, format_report
from src.backtest.engine import run_backtest
from src.data.cache import load_universe
from src.data.edinet_cache import load_negatives, cache_stats as edinet_cache_stats

logger = logging.getLogger("backtest.phase1b")


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    p = argparse.ArgumentParser()
    p.add_argument("--start", default="2023-05-01")
    p.add_argument("--end", default="2026-04-20")
    p.add_argument("--k", type=float, default=2.0)
    p.add_argument("--max-positions", type=int, default=10)
    p.add_argument("--position-size", type=float, default=100_000)
    p.add_argument("--no-news", action="store_true", help="news_negative無効でbaseline実行")
    args = p.parse_args()

    start_d = date.fromisoformat(args.start)
    end_d = date.fromisoformat(args.end)

    logger.info("EDINET cache: %s", edinet_cache_stats())

    thresholds = compute_sector_thresholds_from_cache(k=args.k)
    logger.info("thresholds: %d sectors", len(thresholds))

    universe = load_universe()
    neg_df = None
    if not args.no_news:
        neg_df = load_negatives(list(universe["Code"]), start_d, end_d)
        logger.info("negative disclosures loaded: %d rows, %d tickers",
                    len(neg_df), neg_df["ticker4"].nunique() if len(neg_df) else 0)

    result = run_backtest(
        sector_thresholds=thresholds,
        start_date=args.start, end_date=args.end,
        max_concurrent_positions=args.max_positions,
        position_size=args.position_size,
        news_negatives=neg_df,
    )
    report = format_report(result, thresholds)
    print(report)
    tag = "nonews" if args.no_news else "phase1b"
    out = Path(f"/tmp/bt_{tag}.txt")
    out.write_text(report)
    # trades json
    trades = [{
        "code": t.code, "entry": str(t.entry_date), "exit": str(t.exit_date) if t.exit_date else None,
        "pnl_pct": t.pnl_pct, "reason": t.exit_reason, "consensus": t.consensus_at_entry,
        "hold_days": t.hold_days
    } for t in result.trades]
    Path(f"/tmp/bt_{tag}.json").write_text(json.dumps({"metrics": result.metrics(), "trades": trades}, default=str, indent=2))


if __name__ == "__main__":
    main()
