"""BNFチューニング測定 — consensus×regime 6セルグリッド。

目的: Grove 「BNFで利益出したい (=発火させる)」決定を受けて、
consensus_min と regime gate の2軸で trade-off を実測する。

軸:
  - consensus_min ∈ {3, 4}
  - regimes ∈ {('bearish',), ('bearish','ranging'), None=all}

固定:
  - stop_loss = -0.07, max_hold = 15 (前回 grid best)
  - max_concurrent_positions = 10
  - sector threshold k = 2.0 (既存runnerデフォルト)
"""
from __future__ import annotations

import json
import logging
import time
from pathlib import Path

from src.backtest.runner_w import single

logging.basicConfig(level=logging.WARNING, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")


def main():
    grid = []
    for consensus in [3, 4]:
        for regs in [("bearish",), ("bearish", "ranging"), None]:
            grid.append({"consensus": consensus, "regimes": regs})

    rows = []
    for cfg in grid:
        t0 = time.time()
        m, _, _ = single(
            regimes=cfg["regimes"],
            k=2.0,
            max_hold=15,
            consensus=cfg["consensus"],
            stop_loss=-0.07,
            max_positions=10,
        )
        elapsed = time.time() - t0
        row = {
            "consensus_min": cfg["consensus"],
            "regimes": "+".join(cfg["regimes"]) if cfg["regimes"] else "all",
            "trades": m.get("total_trades", 0),
            "closed": m.get("closed", 0),
            "win_rate": m.get("win_rate"),
            "avg_return": m.get("avg_return"),
            "sharpe": m.get("sharpe_per_trade"),
            "max_dd": m.get("max_drawdown"),
            "best": m.get("best"),
            "worst": m.get("worst"),
            "elapsed_s": round(elapsed, 1),
        }
        rows.append(row)
        print(f"DONE c={cfg['consensus']} regimes={row['regimes']:<20} "
              f"trd={row['trades']:>4} WR={(row['win_rate'] or 0)*100:>5.1f}% "
              f"Sharpe={row['sharpe'] or 0:>+.3f} ({elapsed:.1f}s)")

    print("\n=== sorted by Sharpe ===")
    rows_sorted = sorted(rows, key=lambda r: r["sharpe"] or -999, reverse=True)
    header = f"{'c':>2} {'regimes':<20} | {'trd':>4} {'WR':>6} {'avgR':>7} {'Sharpe':>8} {'MaxDD':>8} {'best':>7} {'worst':>7}"
    print(header)
    print("-" * len(header))
    for r in rows_sorted:
        print(f"{r['consensus_min']:>2} {r['regimes']:<20} | "
              f"{r['trades']:>4} "
              f"{(r['win_rate'] or 0)*100:>5.1f}% "
              f"{(r['avg_return'] or 0)*100:>+6.2f}% "
              f"{r['sharpe'] or 0:>+7.3f} "
              f"{(r['max_dd'] or 0)*100:>+7.1f}% "
              f"{(r['best'] or 0)*100:>+6.2f}% "
              f"{(r['worst'] or 0)*100:>+6.2f}%")

    Path("/tmp/bnf_grid_consensus_regime.json").write_text(
        json.dumps(rows_sorted, default=str, indent=2)
    )
    print("\nsaved: /tmp/bnf_grid_consensus_regime.json")


if __name__ == "__main__":
    main()
