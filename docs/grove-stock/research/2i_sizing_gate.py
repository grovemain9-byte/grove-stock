"""2-I S1ゲート: regime-conditional 逆張りsizing の tail-aware ポートフォリオ経路sim。

問い（W1の続き）: 「弱気で厚張り」はポートフォリオ経路の最大DDを生き残るか？
W1(per-trade)は左尾が深く相関暴落(2024-08)に集中と示した。本物のゲート=
ポートフォリオ equity曲線の最大DDを、フラット vs 厚張り vs 尾抑え厚張り で比較。

変種:
  V0  flat            : 全regime ×1.0（現行baseline）
  V1a naive bearish×1.5: 弱気のみ1.5倍、同時保有上限そのまま(=相関尾を増幅)
  V1b naive bearish×2.0: 弱気のみ2.0倍、上限そのまま(=Groveが当初言った素朴版)
  V2a capped  ×2.0 / 弱気同時≤5 : 厚張りだが弱気建玉数を絞る(tail-cap)
  V2b capped  ×2.0 / 弱気同時≤3 : さらに絞る

各変種の比較軸（平均でなく最悪ケース）:
  max_drawdown        : equity曲線の最大DD（相関尾の集中被弾を捕捉）
  total_return        : 期間総リターン（厚張りの上積み）
  dd_2024_aug         : 2024-08暴落窓のDD（W1で最悪10件中7件が集中した窓）
  calmar              : total_return / |max_drawdown|（リスク調整後=尾を払って上積みが残るか）

READ-MOSTLY: 本番config/ロジック未変更。engineのadditive param(default-off)のみ使用。
再現: cd /Users/ryu/grove-stock && PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 PYTHONPATH=. \
      .venv/bin/python docs/grove-stock/research/2i_sizing_gate.py
"""
from __future__ import annotations
import json
from pathlib import Path
import numpy as np
import pandas as pd

from src.backtest.runner import compute_sector_thresholds_from_cache
from src.backtest.engine import run_backtest

START = "2021-05-19"
END = "2026-06-12"
OUT_JSON = "/tmp/2i_gate.json"
AUG_LO, AUG_HI = pd.Timestamp("2024-07-15"), pd.Timestamp("2024-09-15")

VARIANTS = [
    ("V0_flat",        {}),
    ("V1a_naive_x1.5", {"regime_size_multipliers": {"bearish": 1.5}}),
    ("V1b_naive_x2.0", {"regime_size_multipliers": {"bearish": 2.0}}),
    ("V2a_cap5_x2.0",  {"regime_size_multipliers": {"bearish": 2.0}, "bearish_max_concurrent": 5}),
    ("V2b_cap3_x2.0",  {"regime_size_multipliers": {"bearish": 2.0}, "bearish_max_concurrent": 3}),
]


def equity_stats(eq: pd.Series) -> dict:
    """equity曲線から最悪ケース系の指標を出す。"""
    eq = eq.dropna()
    if len(eq) < 2:
        return {}
    eq.index = pd.to_datetime(eq.index)
    peak = eq.cummax()
    dd = (eq - peak) / peak
    total_return = float(eq.iloc[-1] / eq.iloc[0] - 1.0)
    max_dd = float(dd.min())
    # 2024-08暴落窓のDD（相関尾の本丸）
    aug = eq[(eq.index >= AUG_LO) & (eq.index <= AUG_HI)]
    if len(aug) >= 2:
        aug_peak = aug.cummax()
        dd_aug = float(((aug - aug_peak) / aug_peak).min())
    else:
        dd_aug = float("nan")
    # 日次equityリターンの左尾（CVaR5）
    rets = eq.pct_change().dropna().to_numpy()
    cvar5 = float("nan")
    if len(rets):
        thr = np.percentile(rets, 5)
        tail = rets[rets <= thr]
        cvar5 = float(tail.mean()) if len(tail) else float("nan")
    calmar = total_return / abs(max_dd) if max_dd < 0 else float("nan")
    return {
        "total_return": total_return,
        "max_drawdown": max_dd,
        "dd_2024_aug": dd_aug,
        "daily_cvar5": cvar5,
        "calmar": calmar,
    }


def main() -> None:
    print("=" * 92)
    print("2-I S1 gate — regime-conditional 逆張りsizing / portfolio経路sim")
    print(f"period {START}..{END}  base position_size=100k  max_concurrent=10")
    print("=" * 92)
    thresholds = compute_sector_thresholds_from_cache(k=2.0)

    results = {}
    equity_dump = {}
    for name, kw in VARIANTS:
        print(f"\n[run] {name}  {kw}")
        res = run_backtest(
            sector_thresholds=thresholds, start_date=START, end_date=END,
            max_concurrent_positions=10, position_size=100_000,
            initial_balance=1_000_000, **kw,
        )
        m = res.metrics()
        es = equity_stats(res.equity_curve)
        # 弱気trade数（cap効果の確認）
        n_bear = sum(1 for t in res.trades if t.entry_regime == "bearish")
        n_bear_closed = sum(1 for t in res.trades
                            if t.entry_regime == "bearish" and t.pnl_pct is not None)
        row = {
            "closed": m.get("closed", 0),
            "win_rate": m.get("win_rate", 0.0),
            "avg_return": m.get("avg_return", 0.0),
            "worst_trade": m.get("worst", 0.0),
            "n_bearish": n_bear,
            "n_bearish_closed": n_bear_closed,
            **es,
        }
        results[name] = row
        eq = res.equity_curve.dropna()
        eq.index = pd.to_datetime(eq.index)
        equity_dump[name] = {str(d.date()): float(v) for d, v in eq.items()}
        print(f"    closed={row['closed']} bearish={n_bear} "
              f"total_ret={row.get('total_return',0)*100:+.1f}% "
              f"maxDD={row.get('max_drawdown',0)*100:.1f}% "
              f"DD_aug={row.get('dd_2024_aug',0)*100:.1f}% "
              f"calmar={row.get('calmar',0):.2f}")

    # --- scorecard table ---
    print("\n" + "=" * 92)
    print("SCORECARD（平均でなく最悪ケース基準。calmar=総リターン/最大DD）")
    print("=" * 92)
    hdr = (f"{'variant':<16}{'closed':>7}{'bear':>6}{'tot_ret':>9}{'maxDD':>8}"
           f"{'DD_aug':>8}{'dCVaR5':>8}{'calmar':>8}")
    print(hdr)
    print("-" * 92)
    for name, _ in VARIANTS:
        r = results[name]
        print(f"{name:<16}{r['closed']:>7}{r['n_bearish']:>6}"
              f"{r.get('total_return',0)*100:>8.1f}%{r.get('max_drawdown',0)*100:>7.1f}%"
              f"{r.get('dd_2024_aug',0)*100:>7.1f}%{r.get('daily_cvar5',0)*100:>7.2f}%"
              f"{r.get('calmar',0):>8.2f}")

    # --- gate verdict材料（自動結論せず材料を出す）---
    v0, v1b = results["V0_flat"], results["V1b_naive_x2.0"]
    best_cap = max(["V2a_cap5_x2.0", "V2b_cap3_x2.0"],
                   key=lambda k: results[k].get("calmar", -9e9))
    print("\n[gate材料]")
    print(f"  flat calmar={v0.get('calmar',0):.2f} (maxDD {v0.get('max_drawdown',0)*100:.1f}%)")
    print(f"  naive×2 calmar={v1b.get('calmar',0):.2f} (maxDD {v1b.get('max_drawdown',0)*100:.1f}%) "
          f"← 素朴厚張りが尾を増やすか")
    print(f"  best capped={best_cap} calmar={results[best_cap].get('calmar',0):.2f} "
          f"(maxDD {results[best_cap].get('max_drawdown',0)*100:.1f}%) ← 尾抑えで上積みが残るか")

    Path(OUT_JSON).write_text(json.dumps(
        {"scorecard": results, "equity": equity_dump,
         "period": [START, END], "aug_window": [str(AUG_LO.date()), str(AUG_HI.date())]},
        indent=2))
    print(f"\nsaved -> {OUT_JSON}")
    print("SCORECARD_JSON:", json.dumps({k: {kk: round(vv, 4) if isinstance(vv, float) else vv
                                             for kk, vv in v.items()} for k, v in results.items()}))


if __name__ == "__main__":
    main()
