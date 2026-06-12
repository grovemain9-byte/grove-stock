"""G4過学習検証: sweep最有力候補 vs G3 を年次サブ期間 walk-forward 比較。

sweep で p4_required=True が Sharpe を3倍化したが、単一3年 in-sample。
3つの ~1年窓で out-of-sample 的に G3 と候補を比較し、優位が**複数期間で
持続**するか確認（持続=構造的改善、1窓だけ=レジーム特異/過学習）。
全 cost込・同条件。
"""
from __future__ import annotations

import logging

from src.backtest.runner import compute_sector_thresholds_from_cache
from src.backtest.engine import run_backtest

# 3年cache(2023-04-24〜2026-05-15)を約1年×3窓に分割（purged: 窓境界に
# max_hold=15営業日相当の空白は engine の保有跨ぎで自然吸収。簡易wf）
WINDOWS = [
    ("2023-05-01", "2024-04-30", "Y1"),
    ("2024-05-01", "2025-04-30", "Y2"),
    ("2025-05-01", "2026-05-15", "Y3"),
]
CONFIGS = {
    "G3基準(c3/-7%/p4F)": dict(consensus_min=3, stop_loss=-0.07, p4_required=False),
    "候補(c4/-7%/p4T)":   dict(consensus_min=4, stop_loss=-0.07, p4_required=True),
    "候補2(c3/-10%/p4T)": dict(consensus_min=3, stop_loss=-0.10, p4_required=True),
}


def _bt(th, start, end, cfg):
    m = run_backtest(
        sector_thresholds=th, max_concurrent_positions=7, p4_threshold=0.0,
        start_date=start, end_date=end, slippage_bps=10.0,
        apply_commission=True, **cfg,
    ).metrics()
    if m.get("closed", 0) == 0:
        return None
    return m


def main():
    logging.disable(logging.WARNING)
    th = compute_sector_thresholds_from_cache(k=2.0)
    print("=== walk-forward: 年次サブ期間で G3 vs 候補（全 cost込）===")
    for name, cfg in CONFIGS.items():
        print(f"\n[{name}]")
        print(f"  {'窓':>4} {'期間':>22} {'決着':>5} {'勝率':>6} "
              f"{'avg%':>7} {'maxDD%':>8} {'Sharpe':>7}")
        wins_vs = []
        for s, e, tag in WINDOWS:
            m = _bt(th, s, e, cfg)
            if m is None:
                print(f"  {tag:>4} {s}~{e} : 取引なし")
                continue
            print(f"  {tag:>4} {s}~{e[:7]} {m['closed']:>5} "
                  f"{m['win_rate']*100:>5.1f}% {m['avg_return']*100:>+6.3f} "
                  f"{m['max_drawdown']*100:>7.2f} "
                  f"{m.get('sharpe_per_trade',0):>7.3f}")
            wins_vs.append((tag, m.get("sharpe_per_trade", 0.0),
                            m["max_drawdown"], m["closed"]))
        if name != "G3基準(c3/-7%/p4F)" and wins_vs:
            print(f"  → 全窓 Sharpe: {[f'{t}:{s:.3f}' for t,s,_,_ in wins_vs]}")
    print("\n判定指針: 候補が**全3窓**で G3 同期間より Sharpe↑ かつ "
          "maxDD悪化せず なら構造的改善。1-2窓のみなら過学習/レジーム特異。")
    print("注: 取引数(決着)も併記＝p4=Trueはデータ生成量が半減する代償あり。")


if __name__ == "__main__":
    main()
