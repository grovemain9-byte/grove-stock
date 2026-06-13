"""2-D S1ゲート: 撤退速度 (faster/tighter exit) の edge検証。

問い: 「より速く・より厳しく切る」ことで既存逆張り戦略のリスク調整後リターンが改善するか？

変種:
  A. パラメータスイープ: stop_loss ∈ {-0.03, -0.05, -0.07} × max_hold_days ∈ {3, 5}
     baseline = stop_loss=-0.05 / max_hold_days=5
  B. rebound-fail early-exit: baseline trades を後処理で「N日後にentry価格を上回っていなければ撤退」
     → engineを変更せず、baseline trade ledger + bar dataで再計算

方法論上の注意 (2-Iの教訓):
  - calmarは CAGR/|maxDD| で年率換算（5yr生rawは ~6.6x 水増し）
  - コストOFF比較 + コストON頑健性パス
  - maxDD駆動日(2024-08暴落)を明示
  - CIが重複していれば「差なし」と判定

再現:
  cd /Users/ryu/grove-stock
  PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 PYTHONPATH=. .venv/bin/python docs/grove-stock/research/2d_exit_gate.py
"""
from __future__ import annotations

import json
import math
from datetime import date, timedelta
from pathlib import Path

import numpy as np
import pandas as pd

from src.backtest.runner import compute_sector_thresholds_from_cache
from src.backtest.engine import run_backtest, Trade

START = "2021-05-19"
END = "2026-06-12"
OUT_JSON = "/tmp/2d_gate.json"

# 5年間の取引日数（近似）
TRADING_DAYS_PER_YEAR = 245
TOTAL_YEARS = 5.0

# 2024-08 暴落窓
AUG_LO = pd.Timestamp("2024-07-15")
AUG_HI = pd.Timestamp("2024-09-15")

Z95 = 1.959963984540054  # 95% CI z-score


# ---------------------------------------------------------------------------
# CI helpers (from 2b template)
# ---------------------------------------------------------------------------

def wilson_ci(k: int, n: int, z: float = Z95) -> tuple[float, float]:
    """勝率の Wilson score 95%CI。"""
    if n == 0:
        return (float("nan"), float("nan"))
    p = k / n
    denom = 1 + z * z / n
    center = (p + z * z / (2 * n)) / denom
    half = (z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n))) / denom
    return (center - half, center + half)


def mean_ci(x: np.ndarray, z: float = Z95) -> tuple[float, float, float]:
    """平均の 95%CI。returns (mean, lo, hi)。"""
    n = len(x)
    if n == 0:
        return (float("nan"), float("nan"), float("nan"))
    m = float(x.mean())
    if n == 1:
        return (m, float("nan"), float("nan"))
    se = float(x.std(ddof=1)) / math.sqrt(n)
    return (m, m - z * se, m + z * se)


# ---------------------------------------------------------------------------
# Equity-curve derived metrics (annualized)
# ---------------------------------------------------------------------------

def equity_stats(eq: pd.Series, label: str = "") -> dict:
    """equity曲線からtail-aware指標を計算。calmarはCAGR基準で年率換算。"""
    eq = eq.dropna()
    if len(eq) < 2:
        return {}
    eq.index = pd.to_datetime(eq.index)
    peak = eq.cummax()
    dd = (eq - peak) / peak

    total_return = float(eq.iloc[-1] / eq.iloc[0] - 1.0)
    max_dd = float(dd.min())
    max_dd_date = str(dd.idxmin().date()) if max_dd < 0 else "n/a"

    # CAGR (年率換算)
    n_days = (eq.index[-1] - eq.index[0]).days
    years = max(n_days / 365.25, 0.01)
    cagr = float((eq.iloc[-1] / eq.iloc[0]) ** (1.0 / years) - 1.0)

    # Annualized Sharpe from daily returns
    daily_rets = eq.pct_change().dropna().to_numpy()
    if len(daily_rets) > 1:
        ann_sharpe = float(daily_rets.mean() / daily_rets.std(ddof=1) * math.sqrt(TRADING_DAYS_PER_YEAR))
    else:
        ann_sharpe = float("nan")

    # CAGR-based calmar (年率)
    calmar = cagr / abs(max_dd) if max_dd < 0 else float("nan")

    # 2024-08 暴落窓DD
    aug = eq[(eq.index >= AUG_LO) & (eq.index <= AUG_HI)]
    if len(aug) >= 2:
        aug_peak = aug.cummax()
        dd_aug = float(((aug - aug_peak) / aug_peak).min())
    else:
        dd_aug = float("nan")

    # CVaR5 (daily)
    thr = np.percentile(daily_rets, 5) if len(daily_rets) else 0
    tail = daily_rets[daily_rets <= thr]
    cvar5 = float(tail.mean()) if len(tail) else float("nan")

    return {
        "total_return": total_return,
        "cagr": cagr,
        "ann_sharpe": ann_sharpe,
        "max_drawdown": max_dd,
        "max_dd_date": max_dd_date,
        "dd_2024_aug": dd_aug,
        "daily_cvar5": cvar5,
        "calmar_cagr": calmar,
    }


def trade_stats(trades: list[Trade]) -> dict:
    """closed tradeから勝率/avg_return + Wilson CI。"""
    closed = [t for t in trades if t.pnl_pct is not None]
    if not closed:
        return {"closed": 0}
    pnls = np.array([t.pnl_pct for t in closed])
    n = len(pnls)
    k = int((pnls > 0).sum())
    wr = k / n
    wlo, whi = wilson_ci(k, n)
    amean, alo, ahi = mean_ci(pnls)
    reasons = {}
    for t in closed:
        r = t.exit_reason or "unknown"
        reasons[r] = reasons.get(r, 0) + 1
    return {
        "closed": n,
        "win_rate": wr,
        "win_rate_ci": [wlo, whi],
        "avg_return": amean,
        "avg_return_ci": [alo, ahi],
        "exit_reasons": reasons,
    }


# ---------------------------------------------------------------------------
# Part B: rebound-fail post-process on baseline trade ledger
# ---------------------------------------------------------------------------

def rebound_fail_exit(
    trades: list[Trade],
    n_fail_days: int,
    start: str,
    end: str,
) -> list[Trade]:
    """Baseline trade ledgerを後処理: 保有N日後に entry_price を上回っていなければ
    その日の close で撤退 (exit_reason='rebound_fail')。

    entry_price を上回っていれば元のエンジン決定を維持。
    bar dataはキャッシュから取得。
    """
    from src.data.cache import load_bars

    # コードごとにbarをキャッシュ（重複ロード回避）
    bar_cache: dict[str, pd.DataFrame] = {}

    new_trades: list[Trade] = []
    for t in trades:
        if t.pnl_pct is None:
            # open trade → スキップ
            new_trades.append(t)
            continue

        # rebound-fail check 対象: hold_days > n_fail_days で、exit_reasonがtimeout系
        # （stop_loss/take_profitで既に素早く出た場合は変更不要）
        code = t.code
        if code not in bar_cache:
            df = load_bars(code, start=start, end=end)
            if df is not None and not df.empty:
                df = df.sort_values("date").reset_index(drop=True)
                bar_cache[code] = df
            else:
                bar_cache[code] = pd.DataFrame()

        df = bar_cache.get(code, pd.DataFrame())
        if df.empty or t.hold_days is None or t.hold_days <= n_fail_days:
            new_trades.append(t)
            continue

        # 元のexitがstop_loss/take_profitで既に速い → そのまま
        reason = t.exit_reason or ""
        if reason in ("stop_loss", "take_profit"):
            new_trades.append(t)
            continue

        # N日目のバーを探す
        entry_dt = pd.Timestamp(t.entry_date)
        # entry_dateの後のbarsをN日目まで取得
        after = df[df["date"] > entry_dt].head(n_fail_days)
        if len(after) < n_fail_days:
            # データ不足 → 元のトレードを維持
            new_trades.append(t)
            continue

        nth_bar = after.iloc[-1]  # N日目
        nth_close = float(nth_bar["close"])
        nth_date = nth_bar["date"]

        if isinstance(nth_date, pd.Timestamp):
            nth_date_obj = nth_date.date()
        else:
            nth_date_obj = nth_date

        # N日後にentry価格を上回っていなければ撤退
        if nth_close <= t.entry_price:
            new_t = Trade(
                code=t.code,
                entry_date=t.entry_date,
                entry_price=t.entry_price,
                exit_date=nth_date_obj,
                exit_price=nth_close,
                exit_reason="rebound_fail",
                consensus_at_entry=t.consensus_at_entry,
                pnl_pct=float((nth_close - t.entry_price) / t.entry_price),
                hold_days=n_fail_days,
                entry_regime=t.entry_regime,
                size_mult=t.size_mult,
            )
            new_trades.append(new_t)
        else:
            new_trades.append(t)

    return new_trades


def equity_from_trades(
    trades: list[Trade],
    initial_balance: float,
    position_size: float,
) -> pd.Series:
    """Closed trade ledger から簡易 equity curve を再構築。
    同日 exit のトレードは同日に反映。open trades は無視。
    equity = initial + cumulative PnL (¥)"""
    closed = [t for t in trades if t.pnl_pct is not None and t.exit_date is not None]
    if not closed:
        return pd.Series(dtype=float)
    records = []
    for t in closed:
        pnl_yen = position_size * t.size_mult * t.pnl_pct
        records.append({"date": pd.Timestamp(t.exit_date), "pnl_yen": pnl_yen})
    df = pd.DataFrame(records).groupby("date")["pnl_yen"].sum().sort_index()
    eq = df.cumsum() + initial_balance
    return eq


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    print("=" * 96)
    print("2-D S1 gate — 撤退速度 (faster/tighter exit) edge検証")
    print(f"period {START}..{END}  baseline: stop_loss=-0.05 / max_hold_days=5")
    print("METHOD GUARDRAILS: annualized Sharpe + CAGR-calmar, cost-OFF then cost-ON pass")
    print("=" * 96)

    thresholds = compute_sector_thresholds_from_cache(k=2.0)

    # -----------------------------------------------------------------------
    # PART A: Parameter sweep
    # -----------------------------------------------------------------------
    print("\n" + "=" * 96)
    print("PART A: パラメータスイープ (stop_loss × max_hold_days)")
    print("=" * 96)

    SWEEP_CONFIGS = [
        # (label, stop_loss, max_hold_days)
        ("SL05_H5 [BASELINE]", -0.05, 5),
        ("SL03_H5",            -0.03, 5),
        ("SL07_H5",            -0.07, 5),
        ("SL05_H3",            -0.05, 3),
        ("SL03_H3",            -0.03, 3),
        ("SL07_H3",            -0.07, 3),
    ]

    sweep_results: dict[str, dict] = {}
    baseline_result = None

    for label, sl, hd in SWEEP_CONFIGS:
        print(f"\n[run] {label}  stop_loss={sl}  max_hold_days={hd}")
        res = run_backtest(
            sector_thresholds=thresholds,
            start_date=START, end_date=END,
            max_concurrent_positions=10,
            position_size=100_000,
            initial_balance=1_000_000,
            stop_loss=sl,
            max_hold_days=hd,
            consensus_min=3,
            take_profit_dev=0.0,
            slippage_bps=0.0,
            apply_commission=False,
        )
        ts = trade_stats(res.trades)
        es = equity_stats(res.equity_curve, label=label)
        row = {**ts, **es}
        sweep_results[label] = row
        if "BASELINE" in label:
            baseline_result = res

        print(f"    closed={ts['closed']}  wr={ts['win_rate']*100:.1f}%"
              f"  avg_ret={ts['avg_return']*100:.3f}%"
              f"  CAGR={es.get('cagr',0)*100:.1f}%"
              f"  ann_sharpe={es.get('ann_sharpe',0):.3f}"
              f"  maxDD={es.get('max_drawdown',0)*100:.1f}% [{es.get('max_dd_date','?')}]"
              f"  calmar={es.get('calmar_cagr',0):.3f}")

    # -----------------------------------------------------------------------
    # PART B: rebound-fail early-exit (post-process on baseline)
    # -----------------------------------------------------------------------
    print("\n" + "=" * 96)
    print("PART B: rebound-fail early-exit (N日後に entry価格未回復 → 撤退)")
    print("Baseline trade ledger を後処理。engine.py 変更なし。")
    print("=" * 96)

    REBOUND_N_VALUES = [2, 3]
    rebound_results: dict[str, dict] = {}

    for n in REBOUND_N_VALUES:
        label = f"RF_N{n}"
        print(f"\n[post-process] {label}: {n}日後未回復 → 撤退")
        modified_trades = rebound_fail_exit(
            baseline_result.trades, n_fail_days=n, start=START, end=END
        )
        ts = trade_stats(modified_trades)
        # equityを再構築（簡易版）
        eq = equity_from_trades(modified_trades, initial_balance=1_000_000, position_size=100_000)
        es = equity_stats(eq, label=label)
        # rebound_fail が実際に発火した件数
        n_rf = sum(1 for t in modified_trades if t.exit_reason == "rebound_fail")
        row = {**ts, **es, "n_rebound_fail_triggered": n_rf}
        rebound_results[label] = row

        print(f"    closed={ts['closed']}  wr={ts['win_rate']*100:.1f}%"
              f"  avg_ret={ts['avg_return']*100:.3f}%"
              f"  CAGR={es.get('cagr',0)*100:.1f}%"
              f"  ann_sharpe={es.get('ann_sharpe',0):.3f}"
              f"  maxDD={es.get('max_drawdown',0)*100:.1f}% [{es.get('max_dd_date','?')}]"
              f"  calmar={es.get('calmar_cagr',0):.3f}"
              f"  [rebound_fail_triggered={n_rf}]")

    # -----------------------------------------------------------------------
    # PART C: Cost-ON robustness pass (baseline vs best config from A)
    # -----------------------------------------------------------------------
    print("\n" + "=" * 96)
    print("PART C: コストON頑健性パス (slippage_bps=10, apply_commission=True)")
    print("baseline vs best sweep configを比較")
    print("=" * 96)

    # identify best sweep config by calmar (excluding baseline)
    non_baseline = {k: v for k, v in sweep_results.items() if "BASELINE" not in k}
    best_label = max(non_baseline, key=lambda k: non_baseline[k].get("calmar_cagr", -999))
    # re-parse best config params
    best_sl = next(sl for lbl, sl, hd in SWEEP_CONFIGS if lbl == best_label)
    best_hd = next(hd for lbl, sl, hd in SWEEP_CONFIGS if lbl == best_label)

    cost_results: dict[str, dict] = {}
    for label, sl, hd in [("BASELINE_cost", -0.05, 5), (f"{best_label}_cost", best_sl, best_hd)]:
        print(f"\n[cost-ON] {label}  stop_loss={sl}  max_hold_days={hd}  slippage=10bps commission=ON")
        res = run_backtest(
            sector_thresholds=thresholds,
            start_date=START, end_date=END,
            max_concurrent_positions=10,
            position_size=100_000,
            initial_balance=1_000_000,
            stop_loss=sl,
            max_hold_days=hd,
            consensus_min=3,
            take_profit_dev=0.0,
            slippage_bps=10,
            apply_commission=True,
        )
        ts = trade_stats(res.trades)
        es = equity_stats(res.equity_curve, label=label)
        row = {**ts, **es}
        cost_results[label] = row
        print(f"    closed={ts['closed']}  wr={ts['win_rate']*100:.1f}%"
              f"  avg_ret={ts['avg_return']*100:.3f}%"
              f"  CAGR={es.get('cagr',0)*100:.1f}%"
              f"  ann_sharpe={es.get('ann_sharpe',0):.3f}"
              f"  maxDD={es.get('max_drawdown',0)*100:.1f}%"
              f"  calmar={es.get('calmar_cagr',0):.3f}")

    # -----------------------------------------------------------------------
    # SCORECARD
    # -----------------------------------------------------------------------
    print("\n" + "=" * 96)
    print("SCORECARD (cost-OFF / annualized metrics)")
    print("=" * 96)
    print(f"{'config':<24} {'N':>5} {'WR%':>6} {'WR_CI_lo':>9} {'WR_CI_hi':>9}"
          f" {'avg_ret%':>9} {'CAGR%':>7} {'AnnSharpe':>10} {'maxDD%':>7} {'DD_date':>12} {'calmar':>7}")
    print("-" * 96)

    baseline_row = sweep_results["SL05_H5 [BASELINE]"]
    all_rows = list(sweep_results.items()) + list(rebound_results.items())
    for label, r in all_rows:
        wci = r.get("win_rate_ci", [float("nan"), float("nan")])
        flag = " ★" if label != "SL05_H5 [BASELINE]" and r.get("calmar_cagr", 0) > baseline_row.get("calmar_cagr", 0) else ""
        print(f"{label:<24} {r.get('closed',0):>5}"
              f" {r.get('win_rate',0)*100:>5.1f}%"
              f" {wci[0]*100:>8.1f}%"
              f" {wci[1]*100:>8.1f}%"
              f" {r.get('avg_return',0)*100:>8.3f}%"
              f" {r.get('cagr',0)*100:>6.1f}%"
              f" {r.get('ann_sharpe',0):>9.3f}"
              f" {r.get('max_drawdown',0)*100:>6.1f}%"
              f" {r.get('max_dd_date','?'):>12}"
              f" {r.get('calmar_cagr',0):>6.3f}{flag}")

    print("\n[Cost-ON robustness]")
    print(f"{'config':<30} {'N':>5} {'WR%':>6} {'avg_ret%':>9} {'CAGR%':>7} {'AnnSharpe':>10} {'maxDD%':>7} {'calmar':>7}")
    print("-" * 80)
    for label, r in cost_results.items():
        print(f"{label:<30} {r.get('closed',0):>5}"
              f" {r.get('win_rate',0)*100:>5.1f}%"
              f" {r.get('avg_return',0)*100:>8.3f}%"
              f" {r.get('cagr',0)*100:>6.1f}%"
              f" {r.get('ann_sharpe',0):>9.3f}"
              f" {r.get('max_drawdown',0)*100:>6.1f}%"
              f" {r.get('calmar_cagr',0):>6.3f}")

    # -----------------------------------------------------------------------
    # CI overlap analysis — is any config CLEARLY better than baseline?
    # -----------------------------------------------------------------------
    print("\n[CI重複分析: win_rate CIが重複していれば「差なし」]")
    bci = baseline_row.get("win_rate_ci", [float("nan"), float("nan")])
    for label, r in all_rows:
        if "BASELINE" in label:
            continue
        ci = r.get("win_rate_ci", [float("nan"), float("nan")])
        overlap = not (ci[1] < bci[0] or bci[1] < ci[0])
        delta_wr = r.get("win_rate", 0) - baseline_row.get("win_rate", 0)
        delta_calmar = r.get("calmar_cagr", 0) - baseline_row.get("calmar_cagr", 0)
        print(f"  {label:<24}  WR_delta={delta_wr*100:+.1f}pt  CI_overlap={overlap}"
              f"  calmar_delta={delta_calmar:+.3f}")

    # -----------------------------------------------------------------------
    # Save JSON
    # -----------------------------------------------------------------------
    out = {
        "module": "2-D",
        "period": [START, END],
        "sweep_results": {k: {kk: (round(vv, 5) if isinstance(vv, float) else vv)
                               for kk, vv in v.items()}
                          for k, v in sweep_results.items()},
        "rebound_results": {k: {kk: (round(vv, 5) if isinstance(vv, float) else vv)
                                 for kk, vv in v.items()}
                            for k, v in rebound_results.items()},
        "cost_on_results": {k: {kk: (round(vv, 5) if isinstance(vv, float) else vv)
                                 for kk, vv in v.items()}
                            for k, v in cost_results.items()},
    }
    Path(OUT_JSON).write_text(json.dumps(out, indent=2))
    print(f"\nsaved -> {OUT_JSON}")

    # machine-readable final line for S1 aggregator
    machine = {
        "baseline": {k: round(v, 5) if isinstance(v, float) else v
                     for k, v in baseline_row.items() if not isinstance(v, list)},
        "best_sweep": best_label,
        "best_sweep_calmar": round(sweep_results[best_label].get("calmar_cagr", 0), 5),
        "baseline_calmar": round(baseline_row.get("calmar_cagr", 0), 5),
    }
    print("2D_GATE_JSON:", json.dumps(machine))


if __name__ == "__main__":
    main()
