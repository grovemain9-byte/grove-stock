"""BNF 発火アンロック測定 — P4必須/任意 × consensus × regime gate。

Elite Team Phase 2 測定: 「現相場 (+9%) でも発火する設計」の探索。

軸:
  - p4_required ∈ {True, False}
  - consensus_min ∈ {3, 4}
  - allowed_regimes ∈ {('bearish',), ('bearish','ranging'), None}

固定: stop=-0.07, max_hold=15, max_pos=10, k=2.0
"""
from __future__ import annotations

import json
import logging
import math
import time
from pathlib import Path

import pandas as pd
from src.backtest import engine as engine_mod
from src.backtest.engine import run_backtest
from src.backtest.runner import compute_sector_thresholds_from_cache

logging.basicConfig(level=logging.WARNING, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")

# yfinance rate-limit回避: 事前cacheしたNikkei (CSV) を使う
_NIKKEI_CACHE = pd.read_csv("/tmp/nikkei_5y_cache.csv", parse_dates=["date"])
_NIKKEI_CACHE["date"] = _NIKKEI_CACHE["date"].dt.date


def _load_nikkei_cached() -> pd.DataFrame:
    return _NIKKEI_CACHE.copy()


engine_mod._load_nikkei_bars = _load_nikkei_cached


def main():
    thresholds = compute_sector_thresholds_from_cache(k=2.0)
    grid = []
    for p4_req in [True, False]:
        for consensus in [3, 4]:
            for regs in [("bearish",), ("bearish", "ranging"), None]:
                grid.append({"p4_required": p4_req, "consensus": consensus, "regimes": regs})

    rows = []
    for cfg in grid:
        t0 = time.time()
        result = run_backtest(
            sector_thresholds=thresholds,
            allowed_regimes=set(cfg["regimes"]) if cfg["regimes"] else None,
            max_hold_days=15,
            consensus_min=cfg["consensus"],
            stop_loss=-0.07,
            max_concurrent_positions=10,
            p4_required=cfg["p4_required"],
        )
        m = result.metrics()
        elapsed = time.time() - t0
        n = m.get("closed", 0) or 0
        s = m.get("sharpe_per_trade") or 0
        avg_r = m.get("avg_return") or 0
        dd = abs(m.get("max_drawdown") or 0)
        tpy = n / 3.0
        s_ann = s * math.sqrt(tpy) if tpy > 0 else 0
        ann_ret = avg_r * tpy
        calmar = ann_ret / dd if dd > 0 else 0

        row = {
            "p4_required": cfg["p4_required"],
            "consensus_min": cfg["consensus"],
            "regimes": "+".join(cfg["regimes"]) if cfg["regimes"] else "all",
            "trades": n,
            "win_rate": m.get("win_rate"),
            "avg_return": avg_r,
            "sharpe_per_trade": s,
            "annualized_sharpe": s_ann,
            "annualized_return": ann_ret,
            "max_dd": -dd,
            "calmar": calmar,
            "best": m.get("best"),
            "worst": m.get("worst"),
            "elapsed_s": round(elapsed, 1),
        }
        rows.append(row)
        print(f"DONE p4_req={cfg['p4_required']!s:<5} c={cfg['consensus']} regimes={row['regimes']:<20} "
              f"trd={n:>4} WR={(row['win_rate'] or 0)*100:>5.1f}% "
              f"annS={s_ann:>+.2f} annR={ann_ret*100:>+6.1f}% Calmar={calmar:>5.2f} ({elapsed:.1f}s)")

    print("\n=== sorted by annualized_return ===")
    rows_sorted = sorted(rows, key=lambda r: r["annualized_return"] or 0, reverse=True)
    header = (f"{'p4req':>5} {'c':>2} {'regimes':<20} | "
              f"{'trd':>4} {'WR':>6} {'annR':>8} {'annS':>6} {'Calmar':>7} {'MaxDD':>8}")
    print(header)
    print("-" * len(header))
    for r in rows_sorted:
        print(f"{r['p4_required']!s:>5} {r['consensus_min']:>2} {r['regimes']:<20} | "
              f"{r['trades']:>4} "
              f"{(r['win_rate'] or 0)*100:>5.1f}% "
              f"{r['annualized_return']*100:>+7.1f}% "
              f"{r['annualized_sharpe']:>+6.2f} "
              f"{r['calmar']:>7.2f} "
              f"{r['max_dd']*100:>+7.1f}%")

    Path("/tmp/bnf_grid_p4_unlock.json").write_text(
        json.dumps(rows_sorted, default=str, indent=2)
    )
    print("\nsaved: /tmp/bnf_grid_p4_unlock.json")


if __name__ == "__main__":
    main()
