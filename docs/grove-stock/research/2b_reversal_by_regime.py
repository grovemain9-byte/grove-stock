"""2-B edge検証 Phase A / Track1: 逆張り(BNF)戦略の regime別 backtest。

問い: 逆張りは bullish(上昇トレンド)相場で ranging(もみ合い)相場より明確に負けるか？

手順:
  1. 全history(cache範囲)で逆張りbacktest → trades JSON保存
  2. classify_daily_regimes() → label_trades_by_regime → regime_summary
  3. regime別 win_rate / avg_return / N / sharpe_per_trade に 95%CI併記
  4. regime_day_distribution() で日数分布

READ-MOSTLY: 本番src/config/は変更しない。既存ツール(runner/regime/engine)をreuseする。

再現:
  cd /Users/ryu/grove-stock
  PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 .venv/bin/python docs/grove-stock/research/2b_reversal_by_regime.py
"""
from __future__ import annotations

import json
import math
from datetime import date
from pathlib import Path

import numpy as np
import pandas as pd

from src.backtest.runner import compute_sector_thresholds_from_cache
from src.backtest.engine import run_backtest
from src.backtest.regime import (
    classify_daily_regimes,
    label_trades_by_regime,
    regime_summary,
    regime_day_distribution,
)

# cache全範囲（grove-stock CLAUDE.md / cache_stats基準）
START = "2021-05-19"
END = "2026-06-12"
TRADES_JSON = "/tmp/2b_rev.json"

Z = 1.959963984540054  # 95% normal z


def wilson_ci(k: int, n: int, z: float = Z) -> tuple[float, float]:
    """二項比率の Wilson score 95%CI（正規近似より小N頑健）。"""
    if n == 0:
        return (float("nan"), float("nan"))
    p = k / n
    denom = 1 + z * z / n
    center = (p + z * z / (2 * n)) / denom
    half = (z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n))) / denom
    return (center - half, center + half)


def mean_ci(x: np.ndarray, z: float = Z) -> tuple[float, float, float]:
    """平均の95%CI（正規近似, SE = std/sqrt(n)）。returns (mean, lo, hi)。"""
    n = len(x)
    if n == 0:
        return (float("nan"), float("nan"), float("nan"))
    m = float(x.mean())
    if n == 1:
        return (m, float("nan"), float("nan"))
    se = float(x.std(ddof=1)) / math.sqrt(n)
    return (m, m - z * se, m + z * se)


def bootstrap_winrate_ci(pnls: np.ndarray, n_boot: int = 5000, seed: int = 42) -> tuple[float, float]:
    """勝率の bootstrap 95%CI（Wilsonの裏取り・小N検証用）。"""
    n = len(pnls)
    if n == 0:
        return (float("nan"), float("nan"))
    rng = np.random.default_rng(seed)
    wins = (pnls > 0).astype(float)
    stats = np.empty(n_boot)
    for i in range(n_boot):
        idx = rng.integers(0, n, size=n)
        stats[i] = wins[idx].mean()
    return (float(np.percentile(stats, 2.5)), float(np.percentile(stats, 97.5)))


def main() -> None:
    print("=" * 72)
    print("2-B Track1: 逆張り(BNF) regime別 backtest")
    print(f"period: {START} .. {END}  (cache全範囲)")
    print("=" * 72)

    # --- 1. backtest -------------------------------------------------------
    print("\n[1] sector thresholds (k=2.0)...")
    thresholds = compute_sector_thresholds_from_cache(k=2.0)
    print(f"    {len(thresholds)} sectors")

    print("[1] run_backtest over full history...")
    result = run_backtest(
        sector_thresholds=thresholds,
        start_date=START,
        end_date=END,
        max_concurrent_positions=10,
        position_size=100_000,
        initial_balance=1_000_000,
    )
    m = result.metrics()
    print(f"    total_trades={m.get('total_trades',0)} closed={m.get('closed',0)} "
          f"win_rate={m.get('win_rate',0)*100:.1f}% avg_return={m.get('avg_return',0)*100:.2f}%")

    # save trades JSON in the schema label_trades_by_regime expects
    trades_data = [
        {"code": t.code, "entry": str(t.entry_date),
         "exit": str(t.exit_date) if t.exit_date else None,
         "pnl_pct": t.pnl_pct, "reason": t.exit_reason,
         "consensus": t.consensus_at_entry, "hold_days": t.hold_days}
        for t in result.trades
    ]
    Path(TRADES_JSON).write_text(
        json.dumps({"metrics": m, "thresholds": thresholds, "trades": trades_data},
                   default=str, indent=2)
    )
    print(f"    saved trades -> {TRADES_JSON}")

    # --- 2. regime labeling ------------------------------------------------
    print("\n[2] classify_daily_regimes + label_trades_by_regime...")
    regimes = classify_daily_regimes()
    tdf = label_trades_by_regime(TRADES_JSON, regimes)
    print(f"    labeled closed trades: {len(tdf)}")
    print(f"    regime counts (trades): {tdf['regime'].value_counts().to_dict()}")

    summ = regime_summary(tdf)

    # --- 3. CI per regime --------------------------------------------------
    print("\n[3] regime別 win_rate / avg_return + 95%CI")
    print("-" * 100)
    hdr = f"{'regime':<9} {'N':>5} {'win_rate':>9} {'win_rate 95%CI (Wilson)':>26} " \
          f"{'avg_ret':>9} {'avg_ret 95%CI':>22} {'sharpe':>7}"
    print(hdr)
    print("-" * 100)

    machine: dict[str, dict] = {}
    rows_out = []
    for reg in ["bullish", "ranging", "bearish", "unknown"]:
        grp = tdf[tdf["regime"] == reg]
        n = len(grp)
        if n == 0:
            continue
        pnls = grp["pnl_pct"].to_numpy(dtype=float)
        k = int((pnls > 0).sum())
        wr = k / n
        wlo, whi = wilson_ci(k, n)
        blo, bhi = bootstrap_winrate_ci(pnls)
        amean, alo, ahi = mean_ci(pnls)
        srow = summ[summ["regime"] == reg]
        sharpe = float(srow["sharpe_per_trade"].iloc[0]) if len(srow) else 0.0

        flag = "  <-- N<30 (注意)" if n < 30 else ""
        print(f"{reg:<9} {n:>5} {wr*100:>8.1f}% "
              f"[{wlo*100:>6.1f}, {whi*100:>6.1f}]%      "
              f"{amean*100:>7.2f}% [{alo*100:>6.2f}, {ahi*100:>6.2f}]% "
              f"{sharpe:>7.3f}{flag}")

        machine[reg] = {
            "n": n,
            "win_rate": round(wr, 4),
            "avg_return": round(amean, 5),
        }
        rows_out.append({
            "regime": reg, "n": n, "win_rate": wr,
            "win_rate_ci_wilson": [wlo, whi],
            "win_rate_ci_boot": [blo, bhi],
            "avg_return": amean, "avg_return_ci": [alo, ahi],
            "sharpe_per_trade": sharpe,
        })

    # --- 4. day distribution ----------------------------------------------
    print("\n[4] regime 日数分布 (cache全範囲, N偏りの文脈)")
    dist = regime_day_distribution(regimes, start=START, end=END)
    print(dist.to_string())

    # --- KEY ANSWER --------------------------------------------------------
    print("\n" + "=" * 72)
    print("KEY ANSWER: 逆張りは bullish で ranging より負けるか？")
    print("=" * 72)
    b = next((r for r in rows_out if r["regime"] == "bullish"), None)
    rg = next((r for r in rows_out if r["regime"] == "ranging"), None)
    if b and rg:
        wr_overlap = not (b["win_rate_ci_wilson"][1] < rg["win_rate_ci_wilson"][0]
                          or rg["win_rate_ci_wilson"][1] < b["win_rate_ci_wilson"][0])
        ar_overlap = not (b["avg_return_ci"][1] < rg["avg_return_ci"][0]
                          or rg["avg_return_ci"][1] < b["avg_return_ci"][0])
        print(f"bullish: N={b['n']} win_rate={b['win_rate']*100:.1f}% "
              f"avg_ret={b['avg_return']*100:.2f}%")
        print(f"ranging: N={rg['n']} win_rate={rg['win_rate']*100:.1f}% "
              f"avg_ret={rg['avg_return']*100:.2f}%")
        print(f"win_rate CI overlap: {wr_overlap} | avg_return CI overlap: {ar_overlap}")
        print(f"win_rate diff (bullish-ranging): {(b['win_rate']-rg['win_rate'])*100:+.1f}pt")
        print(f"avg_return diff (bullish-ranging): {(b['avg_return']-rg['avg_return'])*100:+.2f}pt")

    # machine-readable last line
    print()
    print("REVERSAL_BY_REGIME:", json.dumps(machine))


def supplementary_p4_off() -> None:
    """補助分析: P4(市場regime必須条件)を外して bullish regimeでの逆張りを観測。

    primary run(as-is)は P4_required=True のため bullish(日経乖離≥+3%)で1件も建たない
    （p4_pass = nk_dev < 0 が AND 必須）。これは戦略の guardrail であって
    「bullish成績」を測れない。2-Bの真の問い『逆張りに地合い依存の弱点があるか』に
    答えるため、P4必須を外した counterfactual を別途観測する（本番ロジックは未変更）。
    """
    print("\n\n" + "#" * 72)
    print("# 補助分析(counterfactual): p4_required=False で bullish成績を観測")
    print("# ※本番ロジックは変えない。逆張りの地合い依存を測るための逆実装")
    print("#" * 72)

    thresholds = compute_sector_thresholds_from_cache(k=2.0)
    result = run_backtest(
        sector_thresholds=thresholds,
        start_date=START, end_date=END,
        max_concurrent_positions=10, position_size=100_000, initial_balance=1_000_000,
        p4_required=False,  # ← P4を強制条件から外す（bullishでも建てられる）
    )
    m = result.metrics()
    print(f"\n[supp] closed={m.get('closed',0)} win_rate={m.get('win_rate',0)*100:.1f}% "
          f"avg_return={m.get('avg_return',0)*100:.2f}%")

    trades_data = [
        {"code": t.code, "entry": str(t.entry_date),
         "exit": str(t.exit_date) if t.exit_date else None,
         "pnl_pct": t.pnl_pct, "reason": t.exit_reason,
         "consensus": t.consensus_at_entry, "hold_days": t.hold_days}
        for t in result.trades
    ]
    supp_json = "/tmp/2b_rev_p4off.json"
    Path(supp_json).write_text(
        json.dumps({"metrics": m, "trades": trades_data}, default=str, indent=2))

    regimes = classify_daily_regimes()
    tdf = label_trades_by_regime(supp_json, regimes)
    print(f"[supp] regime counts (trades): {tdf['regime'].value_counts().to_dict()}")

    print("\n[supp] regime別 win_rate / avg_return + 95%CI (P4 OFF)")
    print("-" * 100)
    print(f"{'regime':<9} {'N':>5} {'win_rate':>9} {'win_rate 95%CI (Wilson)':>26} "
          f"{'avg_ret':>9} {'avg_ret 95%CI':>22} {'sharpe':>7}")
    print("-" * 100)

    supp_rows = []
    supp_machine: dict[str, dict] = {}
    for reg in ["bullish", "ranging", "bearish", "unknown"]:
        grp = tdf[tdf["regime"] == reg]
        n = len(grp)
        if n == 0:
            continue
        pnls = grp["pnl_pct"].to_numpy(dtype=float)
        k = int((pnls > 0).sum())
        wr = k / n
        wlo, whi = wilson_ci(k, n)
        amean, alo, ahi = mean_ci(pnls)
        std = float(pnls.std(ddof=1)) if n > 1 else 0.0
        sharpe = amean / std if std > 0 else 0.0
        flag = "  <-- N<30 (注意)" if n < 30 else ""
        print(f"{reg:<9} {n:>5} {wr*100:>8.1f}% "
              f"[{wlo*100:>6.1f}, {whi*100:>6.1f}]%      "
              f"{amean*100:>7.2f}% [{alo*100:>6.2f}, {ahi*100:>6.2f}]% "
              f"{sharpe:>7.3f}{flag}")
        supp_machine[reg] = {"n": n, "win_rate": round(wr, 4), "avg_return": round(amean, 5)}
        supp_rows.append({"regime": reg, "n": n, "win_rate": wr,
                          "win_rate_ci": [wlo, whi], "avg_return": amean,
                          "avg_return_ci": [alo, ahi]})

    print("\n" + "=" * 72)
    print("KEY ANSWER (counterfactual, P4 OFF): 逆張りは bullish で ranging より負けるか？")
    print("=" * 72)
    b = next((r for r in supp_rows if r["regime"] == "bullish"), None)
    rg = next((r for r in supp_rows if r["regime"] == "ranging"), None)
    if b and rg:
        wr_overlap = not (b["win_rate_ci"][1] < rg["win_rate_ci"][0]
                          or rg["win_rate_ci"][1] < b["win_rate_ci"][0])
        ar_overlap = not (b["avg_return_ci"][1] < rg["avg_return_ci"][0]
                          or rg["avg_return_ci"][1] < b["avg_return_ci"][0])
        print(f"bullish: N={b['n']} win_rate={b['win_rate']*100:.1f}% avg_ret={b['avg_return']*100:.2f}%")
        print(f"ranging: N={rg['n']} win_rate={rg['win_rate']*100:.1f}% avg_ret={rg['avg_return']*100:.2f}%")
        print(f"win_rate diff (bullish-ranging): {(b['win_rate']-rg['win_rate'])*100:+.1f}pt | CI overlap: {wr_overlap}")
        print(f"avg_return diff (bullish-ranging): {(b['avg_return']-rg['avg_return'])*100:+.2f}pt | CI overlap: {ar_overlap}")
    else:
        print("bullish or ranging missing even with P4 OFF.")

    print()
    print("REVERSAL_BY_REGIME_P4OFF:", json.dumps(supp_machine))


if __name__ == "__main__":
    main()
    supplementary_p4_off()
