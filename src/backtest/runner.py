"""バックテストランナー。キャッシュから全処理実行 → レポート出力。
使用:
    python -m src.backtest.runner [--start 2023-04-01] [--end 2026-04-20] [--k 2.0]
"""
from __future__ import annotations

import argparse
import json
import logging
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd

from src.data.cache import load_universe, load_bars
from src.backtest.engine import run_backtest, MA_PERIOD

logger = logging.getLogger("backtest.runner")


def compute_sector_thresholds_from_cache(
    k: float = 2.0,
    min_codes_per_sector: int = 3,
    as_of_date: str | None = None,
) -> dict[str, float]:
    """キャッシュ済み履歴データから、S33業種別の日次リターン標準偏差を算出 → 閾値 = -k*σ。

    Args:
        as_of_date: ISO-8601 ("YYYY-MM-DD"). 指定時は各銘柄の bar をその日以前に
            切って σ を計算する。characterization test の再現性確保用（cache 増分で
            sector_thresholds が drift し test_c3_baseline_frozen_g3 が壊れる問題への対策）。
    """
    universe = load_universe()
    result: dict[str, float] = {}
    for sector, grp in universe.groupby("S33Nm"):
        sigmas = []
        for code in grp["Code"]:
            df = load_bars(code, end=as_of_date)
            if len(df) < 60:
                continue
            ret = df["close"].pct_change().dropna()
            if len(ret) < 30:
                continue
            sigmas.append(float(ret.std()))
        if len(sigmas) < min_codes_per_sector:
            continue
        avg = float(np.mean(sigmas))
        th = max(-0.20, min(-0.03, -k * avg))
        result[sector] = th
    return result


def format_report(result, thresholds: dict[str, float]) -> str:
    m = result.metrics()
    lines = []
    lines.append("=" * 60)
    lines.append("BNF式 TOPIX500 バックテスト レポート")
    lines.append("=" * 60)
    lines.append(f"total_trades: {m.get('total_trades',0)}")
    lines.append(f"closed: {m.get('closed',0)}")
    if m.get("closed", 0) > 0:
        lines.append(f"wins/losses: {m['wins']}/{m['losses']}")
        lines.append(f"win_rate: {m['win_rate']*100:.1f}%")
        lines.append(f"avg_return/trade: {m['avg_return']*100:.2f}%")
        total_pnl_pct = m['avg_return'] * m['closed']
        annualized = m['avg_return'] * (m['closed'] / 3.0)  # 3年想定
        lines.append(f"total_pnl (sum): {total_pnl_pct*100:+.1f}% (positionサイズ一定前提)")
        lines.append(f"annualized_return (trades×avg/3yr): {annualized*100:+.2f}%/year")
        lines.append(f"median_return: {m['median_return']*100:.2f}%")
        lines.append(f"best/worst: {m['best']*100:+.1f}% / {m['worst']*100:+.1f}%")
        lines.append(f"std: {m['std']*100:.2f}%")
        lines.append(f"sharpe_per_trade: {m.get('sharpe_per_trade',0):.3f}")
        lines.append(f"max_drawdown: {m.get('max_drawdown',0)*100:.1f}%")
        lines.append("")
        lines.append("exit_reasons:")
        for k_, v in m["exit_reasons"].items():
            lines.append(f"  {k_}: {v}")
    lines.append("")
    lines.append(f"sector thresholds used ({len(thresholds)} sectors):")
    for s, v in sorted(thresholds.items(), key=lambda x: x[1]):
        lines.append(f"  {s}: {v*100:.2f}%")
    return "\n".join(lines)


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    p = argparse.ArgumentParser()
    p.add_argument("--start", default=None)
    p.add_argument("--end", default=None)
    p.add_argument("--k", type=float, default=2.0, help="sigma multiplier for sector threshold")
    p.add_argument("--max-positions", type=int, default=10)
    p.add_argument("--position-size", type=float, default=100_000)
    p.add_argument("--initial-balance", type=float, default=1_000_000)
    p.add_argument("--report", default=None, help="path to save report text")
    args = p.parse_args()

    logger.info("computing sector thresholds (k=%.1f)...", args.k)
    thresholds = compute_sector_thresholds_from_cache(k=args.k)
    logger.info("thresholds computed for %d sectors", len(thresholds))

    logger.info("running backtest %s to %s...", args.start or "cache_start", args.end or "cache_end")
    result = run_backtest(
        sector_thresholds=thresholds,
        start_date=args.start,
        end_date=args.end,
        max_concurrent_positions=args.max_positions,
        position_size=args.position_size,
        initial_balance=args.initial_balance,
    )

    report = format_report(result, thresholds)
    print(report)
    if args.report:
        Path(args.report).write_text(report)
    # save trades as JSON
    out_json = Path(args.report or "/tmp/bt_result.json").with_suffix(".json")
    trades_data = [
        {"code": t.code, "entry": str(t.entry_date), "exit": str(t.exit_date) if t.exit_date else None,
         "pnl_pct": t.pnl_pct, "reason": t.exit_reason, "consensus": t.consensus_at_entry,
         "hold_days": t.hold_days}
        for t in result.trades
    ]
    out_json.write_text(json.dumps({"metrics": result.metrics(), "thresholds": thresholds, "trades": trades_data}, default=str, indent=2))
    logger.info("saved trades to %s", out_json)


if __name__ == "__main__":
    main()
