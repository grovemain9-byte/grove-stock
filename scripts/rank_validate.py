"""①ランク選別の効果検証: G3 vs G3+rank を全窓＋年次3窓 walk-forward 比較。

rank_candidates=False(=G3) と True(=辞書順→原理優先順 consensus降/乖離深/
流動性高) を同条件(cost10bps+手数料)で比較。全3窓で改善持続なら構造的
（過学習でなく選別バグ修正の効果）。重み学習していない＝過学習リスク低。
"""
from __future__ import annotations

import logging

from src.backtest.runner import compute_sector_thresholds_from_cache
from src.backtest.engine import run_backtest

WINDOWS = [
    (None, "2026-05-15", "全3年"),
    ("2023-05-01", "2024-04-30", "Y1"),
    ("2024-05-01", "2025-04-30", "Y2"),
    ("2025-05-01", "2026-05-15", "Y3"),
]


def _bt(th, start, end, rank):
    kw = dict(sector_thresholds=th, max_concurrent_positions=7,
              consensus_min=3, p4_required=False, p4_threshold=0.0,
              end_date=end, slippage_bps=10.0, apply_commission=True,
              rank_candidates=rank)
    if start:
        kw["start_date"] = start
    m = run_backtest(**kw).metrics()
    return m if m.get("closed", 0) else None


def main():
    logging.disable(logging.WARNING)
    th = compute_sector_thresholds_from_cache(k=2.0)
    print("=== ①ランク選別: G3(辞書順) vs G3+rank(原理優先) 全cost込 ===")
    print(f"{'窓':>5} | {'G3 決着/勝率/avg/maxDD/Sharpe':>42} | "
          f"{'+rank 決着/勝率/avg/maxDD/Sharpe':>42}")
    print("-" * 96)
    hold = 0
    for s, e, tag in WINDOWS:
        a = _bt(th, s, e, False)
        b = _bt(th, s, e, True)
        if not a or not b:
            print(f"{tag:>5} | データ不足")
            continue
        fa = (f"{a['closed']:>4} {a['win_rate']*100:4.1f}% "
              f"{a['avg_return']*100:+5.2f}% {a['max_drawdown']*100:6.1f}% "
              f"{a.get('sharpe_per_trade',0):5.3f}")
        fb = (f"{b['closed']:>4} {b['win_rate']*100:4.1f}% "
              f"{b['avg_return']*100:+5.2f}% {b['max_drawdown']*100:6.1f}% "
              f"{b.get('sharpe_per_trade',0):5.3f}")
        better = (b.get("sharpe_per_trade", 0) > a.get("sharpe_per_trade", 0)
                  and b["max_drawdown"] >= a["max_drawdown"] - 0.005)
        if tag != "全3年" and better:
            hold += 1
        print(f"{tag:>5} | {fa:>42} | {fb:>42} {'★' if better else ''}")
    print("-" * 96)
    print(f"年次3窓中 {hold}/3 で rank が Sharpe↑&maxDD非悪化。"
          f"3/3=構造的改善・1-2=部分的/レジーム依存。")


if __name__ == "__main__":
    main()
