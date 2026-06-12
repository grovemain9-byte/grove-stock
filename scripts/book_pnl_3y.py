"""5ペーパー資金ブック別・3年過去データ損益（slippage+手数料込み）。

Groveの問い「ペーパー資金を分けてみた過去データでの利益はわからないの？」への
回答。engine.run_backtest（コスト opt-in ON＝Grove確定の現実基準）で3年分の
取引列を生成し、各ブック(¥1M/5M/10M/30M/50M)の資金・適応サイジング
(kelly_size flex/strict)・同時保有7・複利でポートフォリオ再現する。

engine.py は不変（コストは S7b opt-in、サイジングは測定層でブック毎適用）。
"""
from __future__ import annotations

import logging

from src.backtest.runner import compute_sector_thresholds_from_cache
from src.backtest.engine import run_backtest
from src.kelly import kelly_size
from src.data.db import calc_commission
from src.data.cache import load_universe
from config.books import BOOKS


def _simulate_book(trades, initial: float, flex: bool,
                   max_concurrent: int = 7) -> dict:
    """1ブックのポートフォリオ再現。trades は engine の closed Trade 群
    （pnl_pct は既に slippage+手数料控除後）。entry/exit 日でイベント駆動。"""
    evs = sorted(
        [t for t in trades if t.pnl_pct is not None and t.exit_date],
        key=lambda t: t.entry_date)
    equity = initial
    free = initial
    open_pos: list = []  # (exit_date, release_cash, pnl_yen)
    peak = initial
    max_dd = 0.0
    taken = 0
    wins = 0

    def _release(until_date):
        nonlocal equity, free, peak, max_dd, wins
        still = []
        for exit_d, cost_basis, pnl_yen in sorted(open_pos, key=lambda x: x[0]):
            if exit_d <= until_date:
                equity += pnl_yen
                free += cost_basis + pnl_yen
                if pnl_yen > 0:
                    wins += 1
                peak = max(peak, equity)
                if peak > 0:
                    max_dd = min(max_dd, (equity - peak) / peak)
            else:
                still.append((exit_d, cost_basis, pnl_yen))
        open_pos[:] = still

    for t in evs:
        _release(t.entry_date)
        if len(open_pos) >= max_concurrent:
            continue
        price = float(t.entry_price)
        shares = kelly_size(t.consensus_at_entry, equity, price, flex=flex)
        if shares <= 0:
            continue
        cost_basis = shares * price + calc_commission(price, shares)
        if cost_basis > free:
            continue
        free -= cost_basis
        pnl_yen = shares * price * float(t.pnl_pct)  # engine net pnl_pct
        open_pos.append((t.exit_date, cost_basis, pnl_yen))
        taken += 1

    # 残りを最終決済
    if open_pos:
        last = max(e[0] for e in open_pos)
        _release(last)

    return {
        "initial": initial, "final": equity,
        "ret_pct": (equity - initial) / initial * 100.0,
        "trades_taken": taken, "wins": wins,
        "win_rate": (wins / taken * 100.0) if taken else 0.0,
        "max_dd_pct": max_dd * 100.0,
    }


def main():
    logging.disable(logging.WARNING)
    th = compute_sector_thresholds_from_cache(k=2.0)
    res = run_backtest(sector_thresholds=th, max_concurrent_positions=7,
                       consensus_min=3, p4_required=False, p4_threshold=0.0,
                       slippage_bps=10.0, apply_commission=True)
    closed = [t for t in res.trades if t.pnl_pct is not None]
    n_universe = len(load_universe())
    print(f"3年backtest(コスト込10bps+手数料): 決着 {len(closed)}取引 / "
          f"signal universe {n_universe}銘柄")
    print("=" * 78)
    print(f"{'ブック':>6} {'初期':>12} {'最終':>14} {'損益%':>9} "
          f"{'取引数':>6} {'勝率':>6} {'maxDD%':>8}")
    print("-" * 78)
    for bk in BOOKS:
        r = _simulate_book(res.trades, bk.initial_capital, bk.flex)
        tag = "flex" if bk.flex else "固定%"
        print(f"{bk.book_id:>6} ¥{r['initial']:>11,.0f} ¥{r['final']:>13,.0f} "
              f"{r['ret_pct']:>+8.1f}% {r['trades_taken']:>6} "
              f"{r['win_rate']:>5.1f}% {r['max_dd_pct']:>7.1f}%  [{tag}]")
    print("=" * 78)
    print("※ slippage 10bps/片道+立花手数料往復・同時保有7・複利。")
    print("※ engine raw(コスト無)比較: 旧脆い基準は約2倍楽観だった(S7検証済)。")


if __name__ == "__main__":
    main()
