"""戦略強化リサーチ: 1,337universe・コスト込みで主要レバーを grid sweep。

Grove「戦略を強くする案を試す」。G3凍結基準（548/勝率52.9%/avg+0.45%/
maxDD-28.8%/Sharpe0.049）をリスク調整で超える候補を探す。最弱点は
リターンでなく**リスク**（maxDD-28.8%/Sharpe0.049）なので Sharpe/maxDD 重視。

原則:
- 全セル engine cost opt-in ON（slippage10bps+手数料）= G3と同条件で公平比較
- end_date 固定で再現性（cache成長drift排除）
- 12セルに限定（creation MVP・5^N爆発回避）。良候補は別途 walk-forward で
  過学習検証（G4: 単一相場 in-sample 改善を鵜呑みにしない）
"""
from __future__ import annotations

import logging

from src.backtest.runner import compute_sector_thresholds_from_cache
from src.backtest.engine import run_backtest

END = "2026-05-15"  # G3 golden と同窓
# G3 凍結基準（比較対象・固定値）
G3 = dict(closed=548, win=0.5292, avg=0.004502, mdd=-0.28773, sharpe=0.04927)


def _run(th, *, consensus_min, stop_loss, p4_required):
    m = run_backtest(
        sector_thresholds=th, max_concurrent_positions=7,
        consensus_min=consensus_min, p4_required=p4_required,
        p4_threshold=0.0, stop_loss=stop_loss, end_date=END,
        slippage_bps=10.0, apply_commission=True,
    ).metrics()
    if m.get("closed", 0) == 0:
        return None
    return m


def main():
    logging.disable(logging.WARNING)
    th = compute_sector_thresholds_from_cache(k=2.0)
    grid = [
        (c, sl, p4)
        for c in (3, 4)
        for sl in (-0.05, -0.07, -0.10)
        for p4 in (False, True)
    ]
    print(f"=== strategy sweep: 1,337universe cost込 {len(grid)}セル ===")
    print(f"{'c':>2} {'stop':>6} {'p4req':>6} | {'決着':>5} {'勝率':>6} "
          f"{'avg%':>7} {'maxDD%':>8} {'Sharpe':>7} | {'vs G3':>14}")
    print("-" * 78)
    print(f"{'--':>2} {'--':>6} {'--':>6} | {G3['closed']:>5} "
          f"{G3['win']*100:>5.1f}% {G3['avg']*100:>+6.3f} "
          f"{G3['mdd']*100:>7.2f} {G3['sharpe']:>7.3f} | {'(G3基準)':>14}")
    print("-" * 78)
    rows = []
    for c, sl, p4 in grid:
        m = _run(th, consensus_min=c, stop_loss=sl, p4_required=p4)
        if m is None:
            continue
        sh = m.get("sharpe_per_trade", 0.0)
        dd = m["max_drawdown"]
        # リスク調整で G3 を「明確に」上回るか: Sharpe↑ かつ maxDD 悪化せず
        better = sh > G3["sharpe"] and dd >= G3["mdd"]
        rows.append((sh, c, sl, p4, m, better))
        flag = "★改善候補" if better else ("avg↑のみ" if m["avg_return"] > G3["avg"] else "")
        print(f"{c:>2} {sl*100:>5.0f}% {str(p4):>6} | {m['closed']:>5} "
              f"{m['win_rate']*100:>5.1f}% {m['avg_return']*100:>+6.3f} "
              f"{dd*100:>7.2f} {sh:>7.3f} | {flag:>14}")
    print("-" * 78)
    cand = [r for r in rows if r[5]]
    cand.sort(reverse=True)
    if cand:
        print(f"★ Sharpe順 改善候補 {len(cand)}件（要 walk-forward 検証=G4）:")
        for sh, c, sl, p4, m, _ in cand[:3]:
            print(f"   c={c} stop={sl*100:.0f}% p4_required={p4} → "
                  f"Sharpe {sh:.3f}(G3 {G3['sharpe']:.3f}) "
                  f"maxDD {m['max_drawdown']*100:.1f}%(G3 {G3['mdd']*100:.1f}%) "
                  f"avg {m['avg_return']*100:+.3f}%")
    else:
        print("★ G3をリスク調整で明確に上回るセルなし＝現行param妥当 or "
              "別レバー要（in-sample単一相場の限界も考慮）")


if __name__ == "__main__":
    main()
