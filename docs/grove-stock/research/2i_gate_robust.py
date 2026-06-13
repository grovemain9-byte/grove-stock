"""2-I S1ゲート robustness: 「素朴×2が勝つ・capが損」結論への最強の反論を潰す。

メインgate(2i_sizing_gate.py)が示した意外な結論:
  弱気×2 sizing は portfolio最大DDを-19.8%→-27.4%に増やすだけ(破滅でない)、
  calmar 4.54→5.42に改善。tail-cap は勝ち弱気trade を切って価値破壊。

この結論の弱点を2つ潰す（潰せなければ結論は保留）:
  R1 コストON: ×2は2倍のコストdrag。slippage+手数料込みで優位が残るか
     （Phase A/メインgateは cost-off。realistic costが優位を消すか）
  R2 2024-08依存度: maxDD≈DD_aug = 評決が暴落1回(しかも急反発)に依存か。
     equity曲線から Aug窓を除いた maxDD と、Aug窓のリターン寄与を出す。

READ-MOSTLY: 本番未変更。engine additive param のみ。
再現: cd /Users/ryu/grove-stock && PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 PYTHONPATH=. \
      .venv/bin/python docs/grove-stock/research/2i_gate_robust.py
"""
from __future__ import annotations
import json
import numpy as np
import pandas as pd
from src.backtest.runner import compute_sector_thresholds_from_cache
from src.backtest.engine import run_backtest

START, END = "2021-05-19", "2026-06-12"
AUG_LO, AUG_HI = pd.Timestamp("2024-07-15"), pd.Timestamp("2024-09-15")
GATE_JSON = "/tmp/2i_gate.json"

# R1: realistic cost = 片道10bps slippage + 立花往復手数料
COST = {"slippage_bps": 10.0, "apply_commission": True}
COST_VARIANTS = [
    ("V0_flat_cost",   {}),
    ("V1b_x2.0_cost",  {"regime_size_multipliers": {"bearish": 2.0}}),
    ("V2a_cap5_cost",  {"regime_size_multipliers": {"bearish": 2.0}, "bearish_max_concurrent": 5}),
]


def stats(eq: pd.Series) -> dict:
    eq = eq.dropna(); eq.index = pd.to_datetime(eq.index)
    peak = eq.cummax(); dd = (eq - peak) / peak
    tr = float(eq.iloc[-1] / eq.iloc[0] - 1.0); mdd = float(dd.min())
    return {"total_return": tr, "max_drawdown": mdd,
            "calmar": tr / abs(mdd) if mdd < 0 else float("nan")}


def main() -> None:
    # ===== R1 コストON =====
    print("=" * 84)
    print("R1: コストON (片道10bps slippage + 立花往復手数料) — ×2優位はcost後も残るか")
    print("=" * 84)
    thresholds = compute_sector_thresholds_from_cache(k=2.0)
    print(f"{'variant':<16}{'tot_ret':>9}{'maxDD':>8}{'calmar':>8}")
    print("-" * 84)
    cost_rows = {}
    for name, kw in COST_VARIANTS:
        res = run_backtest(sector_thresholds=thresholds, start_date=START, end_date=END,
                           max_concurrent_positions=10, position_size=100_000,
                           initial_balance=1_000_000, **COST, **kw)
        s = stats(res.equity_curve)
        cost_rows[name] = s
        print(f"{name:<16}{s['total_return']*100:>8.1f}%{s['max_drawdown']*100:>7.1f}%{s['calmar']:>8.2f}")

    # ===== R2 2024-08依存度（保存済equityから、再backtest不要）=====
    print("\n" + "=" * 84)
    print("R2: 2024-08暴落への依存度 — Aug窓を除くと maxDD/リターンはどう変わるか")
    print("=" * 84)
    gate = json.loads(open(GATE_JSON).read())
    print(f"{'variant':<16}{'tot_ret':>9}{'maxDD_full':>11}{'maxDD_exAug':>12}{'aug_ret寄与':>11}")
    print("-" * 84)
    r2 = {}
    for name in ["V0_flat", "V1b_naive_x2.0", "V2a_cap5_x2.0"]:
        ed = gate["equity"][name]
        eq = pd.Series({pd.Timestamp(k): v for k, v in ed.items()}).sort_index()
        peak = eq.cummax(); dd = (eq - peak) / peak
        mdd_full = float(dd.min())
        tr_full = float(eq.iloc[-1] / eq.iloc[0] - 1.0)
        # Aug窓を除外した系列でmaxDD（窓の値を前方補完=その間の変動を無効化）
        mask = (eq.index >= AUG_LO) & (eq.index <= AUG_HI)
        eq_ex = eq.copy()
        eq_ex[mask] = np.nan
        eq_ex = eq_ex.ffill().dropna()
        peak_ex = eq_ex.cummax(); dd_ex = (eq_ex - peak_ex) / peak_ex
        mdd_ex = float(dd_ex.min())
        # Aug窓のリターン寄与
        aug = eq[mask]
        aug_ret = float(aug.iloc[-1] / aug.iloc[0] - 1.0) if len(aug) >= 2 else float("nan")
        r2[name] = {"maxDD_full": mdd_full, "maxDD_exAug": mdd_ex, "aug_ret": aug_ret}
        print(f"{name:<16}{tr_full*100:>8.1f}%{mdd_full*100:>10.1f}%{mdd_ex*100:>11.1f}%{aug_ret*100:>10.1f}%")

    print("\n[読み材料]")
    print("  R1: cost後も V1b_cost.calmar > V0_cost.calmar なら ×2優位はcostに頑健")
    print("  R2: maxDD_exAug が full と大差なし＝暴落1回依存でない / 大幅縮小＝Aug依存")
    print("\nROBUST_JSON:", json.dumps({"cost": cost_rows, "aug_dependency": r2}, default=str))


if __name__ == "__main__":
    main()
