"""W: regime-filter backtest + grid search.
python -m src.backtest.runner_w --regimes bearish,ranging --k 2.0 --max-hold 10 --consensus 4 --stop -0.05
python -m src.backtest.runner_w --grid
"""
from __future__ import annotations

import argparse
import json
import logging
from pathlib import Path

from src.backtest.runner import compute_sector_thresholds_from_cache, format_report
from src.backtest.engine import run_backtest

logger = logging.getLogger("backtest.w")


def single(regimes, k, max_hold, consensus, stop_loss, max_positions=10, news_neg=None):
    thresholds = compute_sector_thresholds_from_cache(k=k)
    result = run_backtest(
        sector_thresholds=thresholds,
        allowed_regimes=set(regimes) if regimes else None,
        max_hold_days=max_hold,
        consensus_min=consensus,
        stop_loss=stop_loss,
        max_concurrent_positions=max_positions,
        news_negatives=news_neg,
    )
    return result.metrics(), result, thresholds


def grid_search():
    """最優先グリッド (12通り)。"""
    grid = []
    for regs in [("bearish",), ("bearish","ranging"), None]:
        for mh in [10, 15]:
            for sl in [-0.05, -0.07]:
                grid.append({"regimes": regs, "max_hold": mh, "stop_loss": sl, "max_pos": 10})
    rows = []
    for cfg in grid:
        m, _, _ = single(cfg["regimes"], 2.0, cfg["max_hold"], 4, cfg["stop_loss"], max_positions=cfg["max_pos"])
        rows.append({**cfg, **{k: m.get(k) for k in ("total_trades","closed","win_rate","avg_return","sharpe_per_trade","max_drawdown")}})
    _print_grid(rows)
    Path("/tmp/grid_result.json").write_text(json.dumps(rows, default=str, indent=2))


def grid_search_max_pos():
    """#1 (bearish, mh=15, sl=-0.07) + max_positions dim + news_negatives toggle。"""
    from src.data.edinet_cache import load_negatives
    from src.data.cache import load_universe
    from datetime import date

    universe = load_universe()
    neg = load_negatives(list(universe["Code"]), date(2023,5,1), date(2026,4,20))
    print(f"news_neg rows: {len(neg)}")

    rows = []
    for max_pos in [3, 5, 7, 10]:
        for news_on in [False, True]:
            news_arg = neg if news_on else None
            m, _, _ = single(("bearish",), 2.0, 15, 4, -0.07, max_positions=max_pos, news_neg=news_arg)
            rows.append({"regimes": ("bearish",), "max_hold": 15, "stop_loss": -0.07,
                         "max_pos": max_pos, "news": "yes" if news_on else "no",
                         **{k: m.get(k) for k in ("total_trades","closed","win_rate","avg_return","sharpe_per_trade","max_drawdown")}})
    _print_grid(rows)
    Path("/tmp/grid_maxpos.json").write_text(json.dumps(rows, default=str, indent=2))


def _print_grid(rows):
    rows.sort(key=lambda r: r.get("sharpe_per_trade") or -999, reverse=True)
    has_maxpos = "max_pos" in rows[0]
    has_news = "news" in rows[0]
    header = f"{'regimes':<22}{'mh':>4}{'sl':>6}"
    if has_maxpos: header += f"{'mp':>4}"
    if has_news: header += f"{'news':>5}"
    header += f" | {'trd':>4} {'WR':>6} {'avgR':>7} {'Sharpe':>8} {'MaxDD':>8}"
    print(header)
    for r in rows:
        reg_str = "+".join(r["regimes"]) if r.get("regimes") else "all"
        line = f"{reg_str:<22}{r['max_hold']:>4}{r['stop_loss']:>6.2f}"
        if has_maxpos: line += f"{r.get('max_pos',10):>4}"
        if has_news: line += f"{r.get('news','-'):>5}"
        line += (f" | {r.get('total_trades',0):>4} "
                 f"{(r.get('win_rate',0) or 0)*100:>5.1f}% "
                 f"{(r.get('avg_return',0) or 0)*100:>+6.2f}% "
                 f"{r.get('sharpe_per_trade',0) or 0:>+7.3f} "
                 f"{(r.get('max_drawdown',0) or 0)*100:>+7.1f}%")
        print(line)


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    p = argparse.ArgumentParser()
    p.add_argument("--regimes", default="bearish,ranging", help="comma-sep or 'all'")
    p.add_argument("--k", type=float, default=2.0)
    p.add_argument("--max-hold", type=int, default=10)
    p.add_argument("--consensus", type=int, default=4)
    p.add_argument("--stop", type=float, default=-0.05)
    p.add_argument("--grid", action="store_true")
    p.add_argument("--grid-maxpos", action="store_true")
    args = p.parse_args()

    if args.grid:
        grid_search()
        return
    if args.grid_maxpos:
        grid_search_max_pos()
        return

    regimes = None if args.regimes == "all" else args.regimes.split(",")
    m, result, th = single(regimes, args.k, args.max_hold, args.consensus, args.stop)
    print(format_report(result, th))


if __name__ == "__main__":
    main()
