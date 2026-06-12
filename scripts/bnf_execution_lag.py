"""1日約定lag = 信号 (today close) → 約定 (next-day open) のpost-processing。

Top 0.01% quant の指摘:
  「半日のズレが逆張り戦略のアルファの大半を消し去る」
  → 信号: 大引け後確定
  → 実約定: 翌日寄付き

各 trade を翌日 open ベースで pnl 再計算。
"""
from __future__ import annotations

import json
import logging
import math
import time
from datetime import date, timedelta
from pathlib import Path

import pandas as pd

from src.backtest import engine as engine_mod
from src.backtest.engine import run_backtest, BacktestResult, Trade
from src.backtest.runner import compute_sector_thresholds_from_cache
from src.data.cache import load_bars

logging.basicConfig(level=logging.WARNING, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")

_NIKKEI_CACHE = pd.read_csv("/tmp/nikkei_5y_cache.csv", parse_dates=["date"])
_NIKKEI_CACHE["date"] = _NIKKEI_CACHE["date"].dt.date


def _load_nikkei_cached() -> pd.DataFrame:
    return _NIKKEI_CACHE.copy()


engine_mod._load_nikkei_bars = _load_nikkei_cached


_BAR_CACHE: dict[str, pd.DataFrame] = {}


def _bars(code: str) -> pd.DataFrame:
    if code not in _BAR_CACHE:
        df = load_bars(code).copy()
        df["date"] = pd.to_datetime(df["date"]).dt.date if not isinstance(df["date"].iloc[0], date) else df["date"]
        df = df.sort_values("date").reset_index(drop=True)
        _BAR_CACHE[code] = df
    return _BAR_CACHE[code]


def next_open_after(code: str, d: date) -> tuple[date, float] | None:
    """d より後の最初の営業日 open 価格。"""
    df = _bars(code)
    after = df[df["date"] > d]
    if after.empty:
        return None
    r = after.iloc[0]
    return (r["date"], float(r["open"]))


def apply_execution_lag(result: BacktestResult, *, commission_pct: float, slippage_pct: float) -> BacktestResult:
    new_trades = []
    skipped = 0
    for t in result.trades:
        if t.pnl_pct is None or t.exit_date is None:
            new_trades.append(t)
            continue
        entry_fill = next_open_after(t.code, t.entry_date)
        exit_fill = next_open_after(t.code, t.exit_date)
        if entry_fill is None or exit_fill is None:
            skipped += 1
            continue
        e_date, e_px = entry_fill
        x_date, x_px = exit_fill
        # slippage: buy at +slippage, sell at -slippage
        e_px_eff = e_px * (1 + slippage_pct)
        x_px_eff = x_px * (1 - slippage_pct)
        pnl = (x_px_eff - e_px_eff) / e_px_eff - commission_pct
        new_trades.append(Trade(
            code=t.code,
            entry_date=e_date,
            entry_price=e_px_eff,
            exit_date=x_date,
            exit_price=x_px_eff,
            exit_reason=t.exit_reason,
            consensus_at_entry=t.consensus_at_entry,
            pnl_pct=pnl,
            hold_days=(x_date - e_date).days,
        ))
    print(f"applied lag: kept={len(new_trades)} skipped={skipped}")
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
        "annualized_return": ann_ret,
        "annualized_sharpe": s_ann,
        "max_dd": -dd,
        "calmar": calmar,
        "tpy": tpy,
    }


def print_row(s: dict):
    print(f"{s['label']:<45} | trd={s['trades']:>4} WR={s['win_rate']*100:>5.1f}% "
          f"avgR/trd={s['avg_return_per_trade']*100:>+5.2f}% "
          f"annR={s['annualized_return']*100:>+7.1f}% "
          f"annS={s['annualized_sharpe']:>+5.2f} "
          f"Calmar={s['calmar']:>5.2f} "
          f"DD={s['max_dd']*100:>+6.1f}%")


def main():
    thresholds = compute_sector_thresholds_from_cache(k=2.0)
    commission = 0.0022
    slippage = 0.0010

    common_kwargs = dict(
        sector_thresholds=thresholds,
        allowed_regimes=None,
        max_hold_days=15,
        consensus_min=3,
        stop_loss=-0.07,
        max_concurrent_positions=10,
        p4_required=False,
    )

    print("=== FULL 3年 with execution lag + realistic cost ===")
    t0 = time.time()
    result = run_backtest(**common_kwargs)
    print(f"raw backtest: {time.time()-t0:.1f}s trades={len(result.trades)}")
    s_lag = summarize(
        apply_execution_lag(result, commission_pct=commission, slippage_pct=slippage),
        "FULL lag + 0.42% cost (TRUE realistic)", years=3.0,
    )
    print_row(s_lag)

    print("\n=== Walk-forward OOS (2025-05 〜 2026-05) with lag ===")
    result_oos = run_backtest(start_date="2025-05-01", end_date="2026-05-08", **common_kwargs)
    s_oos_lag = summarize(
        apply_execution_lag(result_oos, commission_pct=commission, slippage_pct=slippage),
        "OOS lag + cost (HONEST production estimate)", years=1.0,
    )
    print_row(s_oos_lag)

    Path("/tmp/bnf_lag.json").write_text(json.dumps({
        "full_lag_cost": s_lag,
        "oos_lag_cost": s_oos_lag,
        "cost": {"commission": commission, "slippage_per_side": slippage},
    }, default=str, indent=2))
    print("\nsaved: /tmp/bnf_lag.json")


if __name__ == "__main__":
    main()
