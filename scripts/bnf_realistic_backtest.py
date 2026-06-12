"""BNF 現実コスト + walk-forward OOS バックテスト。

Elite Team v2 敵対的検証:
  - Commission: 110円/side × 2 = 220円 / 100k position = 0.22% round-trip
  - Slippage: 0.10% × 2 = 0.20% round-trip (TOPIX500 mid-cap想定)
  - Total cost: 0.42% per round-trip
  - Walk-forward: 2023-05 〜 2025-04 (train) / 2025-05 〜 2026-04 (OOS test)

target cell: p4_required=False, consensus_min=3, regimes=all
"""
from __future__ import annotations

import json
import logging
import math
import sys
import time
from datetime import date
from pathlib import Path

import pandas as pd

from src.backtest import engine as engine_mod
from src.backtest.engine import run_backtest, BacktestResult, Trade
from src.backtest.runner import compute_sector_thresholds_from_cache

logging.basicConfig(level=logging.WARNING, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")

# Nikkei cache
_NIKKEI_CACHE = pd.read_csv("/tmp/nikkei_5y_cache.csv", parse_dates=["date"])
_NIKKEI_CACHE["date"] = _NIKKEI_CACHE["date"].dt.date


def _load_nikkei_cached() -> pd.DataFrame:
    return _NIKKEI_CACHE.copy()


engine_mod._load_nikkei_bars = _load_nikkei_cached


def apply_realistic_costs(result: BacktestResult, *, commission_pct: float, slippage_pct: float) -> BacktestResult:
    """各 trade の pnl_pct に約定コストを反映。

    realistic_pnl = pnl_pct - 2*slippage_pct - commission_pct  (round-trip)
    """
    cost_per_trade = 2 * slippage_pct + commission_pct
    new_trades = []
    for t in result.trades:
        if t.pnl_pct is None:
            new_trades.append(t)
            continue
        new_t = Trade(
            code=t.code,
            entry_date=t.entry_date,
            entry_price=t.entry_price,
            exit_date=t.exit_date,
            exit_price=t.exit_price,
            exit_reason=t.exit_reason,
            consensus_at_entry=t.consensus_at_entry,
            pnl_pct=t.pnl_pct - cost_per_trade,
            hold_days=t.hold_days,
        )
        new_trades.append(new_t)
    return BacktestResult(trades=new_trades, params=result.params, equity_curve=result.equity_curve)


def summarize(result: BacktestResult, label: str, years: float) -> dict:
    m = result.metrics()
    n = m.get("closed", 0) or 0
    s = m.get("sharpe_per_trade") or 0
    avg_r = m.get("avg_return") or 0
    dd = abs(m.get("max_drawdown") or 0)
    tpy = n / years if years > 0 else 0
    s_ann = s * math.sqrt(tpy) if tpy > 0 else 0
    ann_ret = avg_r * tpy
    calmar = ann_ret / dd if dd > 0 else 0
    return {
        "label": label,
        "trades": n,
        "win_rate": m.get("win_rate") or 0,
        "avg_return_per_trade": avg_r,
        "std_per_trade": m.get("std") or 0,
        "annualized_return": ann_ret,
        "annualized_sharpe": s_ann,
        "max_dd": -dd,
        "calmar": calmar,
        "best_trade": m.get("best") or 0,
        "worst_trade": m.get("worst") or 0,
        "tpy": tpy,
        "years": years,
    }


def print_row(s: dict):
    print(f"{s['label']:<35} | trd={s['trades']:>4} WR={s['win_rate']*100:>5.1f}% "
          f"avgR/trd={s['avg_return_per_trade']*100:>+5.2f}% "
          f"annR={s['annualized_return']*100:>+7.1f}% "
          f"annS={s['annualized_sharpe']:>+5.2f} "
          f"Calmar={s['calmar']:>5.2f} "
          f"DD={s['max_dd']*100:>+6.1f}%")


def main():
    thresholds = compute_sector_thresholds_from_cache(k=2.0)
    common_kwargs = dict(
        sector_thresholds=thresholds,
        allowed_regimes=None,  # all
        max_hold_days=15,
        consensus_min=3,
        stop_loss=-0.07,
        max_concurrent_positions=10,
        p4_required=False,
    )

    commission = 0.0022  # 110円×2 / 100k = 0.22%
    slippage = 0.0010    # 0.10% (TOPIX500想定、片道)

    # === Full period (2023-05 〜 2026-05) ===
    print("=== A. FULL PERIOD (~3年) ===")
    t0 = time.time()
    result_full = run_backtest(**common_kwargs)
    print(f"backtest elapsed: {time.time()-t0:.1f}s, raw trades={len(result_full.trades)}")
    s_full_raw = summarize(result_full, "FULL raw (no cost)", years=3.0)
    print_row(s_full_raw)
    result_full_real = apply_realistic_costs(result_full, commission_pct=commission, slippage_pct=slippage)
    s_full_real = summarize(result_full_real, "FULL realistic (0.42% RT)", years=3.0)
    print_row(s_full_real)

    # === Walk-forward: train 2023-05 〜 2025-04 (2yr), test 2025-05 〜 2026-05 (1yr) ===
    print("\n=== B. WALK-FORWARD ===")
    train_end = "2025-04-30"
    test_start = "2025-05-01"
    print(f"train: 2023-05 〜 {train_end} (~2年)")
    print(f"test:  {test_start} 〜 2026-05 (~1年, OOS)")

    t0 = time.time()
    result_train = run_backtest(start_date="2023-05-01", end_date=train_end, **common_kwargs)
    print(f"train backtest: {time.time()-t0:.1f}s, trades={len(result_train.trades)}")
    s_train_real = summarize(
        apply_realistic_costs(result_train, commission_pct=commission, slippage_pct=slippage),
        "TRAIN realistic", years=2.0,
    )
    print_row(s_train_real)

    t0 = time.time()
    result_test = run_backtest(start_date=test_start, end_date="2026-05-08", **common_kwargs)
    print(f"test backtest: {time.time()-t0:.1f}s, trades={len(result_test.trades)}")
    s_test_real = summarize(
        apply_realistic_costs(result_test, commission_pct=commission, slippage_pct=slippage),
        "TEST(OOS) realistic", years=1.0,
    )
    print_row(s_test_real)

    # OOS degradation
    deg = (s_test_real["annualized_return"] - s_train_real["annualized_return"]) / max(abs(s_train_real["annualized_return"]), 1e-9)
    print(f"\nOOS degradation (annR): {deg*100:+.1f}% vs train")

    # === Save ===
    Path("/tmp/bnf_realistic.json").write_text(json.dumps({
        "full_raw": s_full_raw,
        "full_realistic": s_full_real,
        "train_realistic": s_train_real,
        "test_realistic": s_test_real,
        "oos_degradation_pct": deg,
        "cost_assumptions": {"commission_round_trip_pct": commission, "slippage_round_trip_pct": slippage * 2},
    }, default=str, indent=2))
    print("\nsaved: /tmp/bnf_realistic.json")


if __name__ == "__main__":
    main()
