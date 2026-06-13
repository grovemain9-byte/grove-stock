"""2-H S1ゲート: trailing exit（目標値なし・ピークから引く） vs fixed exit の edge 検証。

問い: BNFの「目標値なし原則」= trailing exit（ピークから X% 引いた時に撤退）は
      現行固定exit（stop_loss -5% / MA25回帰(take_profit_dev=0) / max 5日 / consensus<3）
      より 5年リターンを改善するか？

手法（apples-to-apples）:
  - baseline の run_backtest で「entries」を確定（entry logic 完全固定）。
  - 各 trade の entry_date 以降の bar を load_bars で再読み込み。
  - trailing exit を post-process: ピーク終値から X% 引き でexit。stop_loss -5% と max_hold
    は trailing の「床」として維持。コスト比較のため trailing は約定数が変わりうる点に注意。
  - sweep X ∈ {3%, 5%, 8%} and max_hold ∈ {10d, 20d}（計 6変種 + baseline = 7行）。

METHODOLOGY GUARDRAILS (2-I 教訓):
  1. ANNUALIZE metrics（raw 5yr total / maxDD は ~6.6x 錯覚源）。
  2. NO LEVERAGE CONFOUND: entry logic・position_size 固定。trailing の変更はHOLD期間のみ。
  3. TAIL-AWARE: maxDD + driving date、2024-08 暴落窓のDD 別報告。
  4. APPLES-TO-APPLES: cost-OFF 比較 + cost-ON 感度（slippage 10bps + commission）。
  5. CIs: bootstrap 95% CI で win_rate / avg_return の有意性チェック（N変化に注意）。
  6. "No improvement" は SUCCESS（安いfail = 建設禁止）。

再現:
  cd /Users/ryu/grove-stock && PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 PYTHONPATH=. \
      .venv/bin/python docs/grove-stock/research/2h_trailing_gate.py
"""
from __future__ import annotations

import json
import math
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path
from typing import Optional

import numpy as np
import pandas as pd

from src.backtest.runner import compute_sector_thresholds_from_cache
from src.backtest.engine import run_backtest, _compute_indicators, Trade
from src.data.cache import load_bars
from src.data.db import calc_commission

START = "2021-05-19"
END   = "2026-06-12"
AUG_LO = pd.Timestamp("2024-07-15")
AUG_HI = pd.Timestamp("2024-09-15")
OUT_JSON = "/tmp/2h_gate.json"

# ---- trailing variants: (trail_pct, max_hold_days) ----
TRAIL_VARIANTS = [
    (0.03, 10),
    (0.03, 20),
    (0.05, 10),
    (0.05, 20),
    (0.08, 10),
    (0.08, 20),
]

STOP_LOSS = -0.05     # same as baseline
COST_SLIP_BPS = 10.0
COST_COMMISSION = True


# ──────────────────────────────────────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────────────────────────────────────

def _round_trip_cost_pct(entry_price: float, pnl: float, position_size: float,
                          slippage_bps: float, apply_commission: bool) -> float:
    if entry_price <= 0 or (slippage_bps <= 0 and not apply_commission):
        return 0.0
    cost = 0.0
    if apply_commission:
        shares = position_size / entry_price
        exit_price = entry_price * (1.0 + pnl)
        cost += (calc_commission(entry_price, shares)
                 + calc_commission(exit_price, shares)) / position_size
    if slippage_bps > 0:
        s = slippage_bps / 10_000.0
        cost += (1.0 + pnl) - (1.0 + pnl) * (1.0 - s) / (1.0 + s)
    return cost


def _load_bar_map(codes: list[str]) -> dict[str, pd.DataFrame]:
    """各銘柄の indicators 付き DataFrame を返す（engine と同じ _compute_indicators を使用）。"""
    bar_map: dict[str, pd.DataFrame] = {}
    for code in codes:
        df = load_bars(code, start=START, end=END)
        if df is None or len(df) < 30:
            continue
        df = _compute_indicators(df)
        df["date"] = pd.to_datetime(df["date"])
        bar_map[code] = df.set_index("date")
    return bar_map


@dataclass
class TradeSim:
    code: str
    entry_date: date
    entry_price: float
    exit_date: Optional[date]
    exit_price: Optional[float]
    exit_reason: Optional[str]
    pnl_pct: Optional[float]
    hold_days: Optional[int]


def simulate_trailing(
    baseline_trade: Trade,
    bar_map: dict[str, pd.DataFrame],
    trail_pct: float,
    max_hold: int,
    apply_cost: bool = False,
    position_size: float = 100_000.0,
) -> TradeSim:
    """baseline の entry を使い trailing exit でシミュレート。"""
    code = baseline_trade.code
    entry_date = baseline_trade.entry_date
    entry_price = baseline_trade.entry_price

    df = bar_map.get(code)
    if df is None:
        # bar なし → baseline pnl をそのまま返す（変わらない）
        return TradeSim(
            code=code, entry_date=entry_date, entry_price=entry_price,
            exit_date=baseline_trade.exit_date, exit_price=baseline_trade.exit_price,
            exit_reason=baseline_trade.exit_reason, pnl_pct=baseline_trade.pnl_pct,
            hold_days=baseline_trade.hold_days,
        )

    entry_ts = pd.Timestamp(entry_date)
    future = df[df.index > entry_ts].copy()
    if future.empty:
        return TradeSim(
            code=code, entry_date=entry_date, entry_price=entry_price,
            exit_date=baseline_trade.exit_date, exit_price=baseline_trade.exit_price,
            exit_reason=baseline_trade.exit_reason, pnl_pct=baseline_trade.pnl_pct,
            hold_days=baseline_trade.hold_days,
        )

    peak = entry_price
    hold = 0

    for ts, row in future.iterrows():
        if hold >= max_hold:
            # max hold 到達 → 翌日始値ではなく当日終値で exit（保守的）
            pnl = (row["close"] - entry_price) / entry_price
            cost = _round_trip_cost_pct(entry_price, pnl, position_size,
                                         COST_SLIP_BPS if apply_cost else 0.0,
                                         COST_COMMISSION if apply_cost else False)
            return TradeSim(
                code=code, entry_date=entry_date, entry_price=entry_price,
                exit_date=ts.date(), exit_price=float(row["close"]),
                exit_reason="max_hold", pnl_pct=float(pnl - cost), hold_days=hold,
            )

        cur_price = float(row["close"])

        # stop loss（床）
        pnl_now = (cur_price - entry_price) / entry_price
        if pnl_now <= STOP_LOSS:
            cost = _round_trip_cost_pct(entry_price, pnl_now, position_size,
                                         COST_SLIP_BPS if apply_cost else 0.0,
                                         COST_COMMISSION if apply_cost else False)
            return TradeSim(
                code=code, entry_date=entry_date, entry_price=entry_price,
                exit_date=ts.date(), exit_price=cur_price,
                exit_reason="stop_loss", pnl_pct=float(pnl_now - cost), hold_days=hold,
            )

        # update peak
        peak = max(peak, cur_price)

        # trailing trigger
        trail_trigger = peak * (1.0 - trail_pct)
        if cur_price <= trail_trigger and hold > 0:  # 初日は除外（すぐに引かない）
            pnl = (cur_price - entry_price) / entry_price
            cost = _round_trip_cost_pct(entry_price, pnl, position_size,
                                         COST_SLIP_BPS if apply_cost else 0.0,
                                         COST_COMMISSION if apply_cost else False)
            return TradeSim(
                code=code, entry_date=entry_date, entry_price=entry_price,
                exit_date=ts.date(), exit_price=cur_price,
                exit_reason="trailing", pnl_pct=float(pnl - cost), hold_days=hold,
            )

        hold += 1

    # 期間末まで保有（open）
    last_ts, last_row = list(future.iterrows())[-1]
    pnl = (float(last_row["close"]) - entry_price) / entry_price
    cost = _round_trip_cost_pct(entry_price, pnl, position_size,
                                 COST_SLIP_BPS if apply_cost else 0.0,
                                 COST_COMMISSION if apply_cost else False)
    return TradeSim(
        code=code, entry_date=entry_date, entry_price=entry_price,
        exit_date=last_ts.date(), exit_price=float(last_row["close"]),
        exit_reason="period_end", pnl_pct=float(pnl - cost), hold_days=hold,
    )


# ──────────────────────────────────────────────────────────────────────────────
# Metrics
# ──────────────────────────────────────────────────────────────────────────────

def bootstrap_ci(x: np.ndarray, stat_fn, n_boot: int = 2000, seed: int = 42,
                 ci: float = 0.95) -> tuple[float, float]:
    rng = np.random.default_rng(seed)
    alpha = (1 - ci) / 2
    stats = [stat_fn(rng.choice(x, size=len(x), replace=True)) for _ in range(n_boot)]
    return (float(np.percentile(stats, alpha * 100)),
            float(np.percentile(stats, (1 - alpha) * 100)))


def trade_stats(sims: list[TradeSim], initial_balance: float = 1_000_000.0,
                position_size: float = 100_000.0) -> dict:
    """Per-trade metrics + annualized equity-curve stats。"""
    closed = [s for s in sims if s.pnl_pct is not None]
    if not closed:
        return {}
    pnls = np.array([s.pnl_pct for s in closed])
    wins = (pnls > 0)
    n = len(pnls)
    wr = float(wins.mean())
    avg_ret = float(pnls.mean())
    std_ret = float(pnls.std(ddof=1)) if n > 1 else 0.0
    sharpe_pt = avg_ret / std_ret if std_ret > 0 else 0.0

    # --- annualized metrics from equity path ---
    # build a simple daily equity curve by replaying trades in chronological order
    # (same sequential model as engine – single slot per dollar)
    eq_df = _build_equity_curve(closed, initial_balance, position_size)
    eq = eq_df["equity"]
    years = (eq.index[-1] - eq.index[0]).days / 365.25 if len(eq) > 1 else 1.0

    total_ret = float(eq.iloc[-1] / eq.iloc[0] - 1.0)
    cagr = float((eq.iloc[-1] / eq.iloc[0]) ** (1.0 / years) - 1.0) if years > 0 else 0.0
    peak = eq.cummax()
    dd = (eq - peak) / peak
    max_dd = float(dd.min())
    max_dd_date = str(dd.idxmin().date()) if not dd.empty else ""

    # 2024-08 window DD
    aug_eq = eq[(eq.index >= AUG_LO) & (eq.index <= AUG_HI)]
    if len(aug_eq) >= 2:
        aug_peak = aug_eq.cummax()
        dd_aug = float(((aug_eq - aug_peak) / aug_peak).min())
    else:
        dd_aug = float("nan")

    calmar_ann = cagr / abs(max_dd) if max_dd < 0 else float("nan")

    # daily returns Sharpe (annualized)
    daily_rets = eq.pct_change().dropna().to_numpy()
    sharpe_ann = float("nan")
    if len(daily_rets) > 1 and daily_rets.std() > 0:
        sharpe_ann = float(daily_rets.mean() / daily_rets.std() * math.sqrt(252))

    # bootstrap CI on avg_return
    wr_ci = bootstrap_ci(pnls, lambda x: (x > 0).mean())
    ret_ci = bootstrap_ci(pnls, np.mean)

    # avg hold
    holds = [s.hold_days for s in closed if s.hold_days is not None]
    avg_hold = float(np.mean(holds)) if holds else float("nan")

    exit_reasons = {}
    for s in closed:
        exit_reasons[s.exit_reason or "unknown"] = exit_reasons.get(s.exit_reason or "unknown", 0) + 1

    return {
        "n_trades": n,
        "win_rate": round(wr, 4),
        "wr_ci95": [round(wr_ci[0], 4), round(wr_ci[1], 4)],
        "avg_return": round(avg_ret, 5),
        "ret_ci95": [round(ret_ci[0], 5), round(ret_ci[1], 5)],
        "std_return": round(std_ret, 5),
        "sharpe_per_trade": round(sharpe_pt, 4),
        "sharpe_ann": round(sharpe_ann, 4),
        "cagr": round(cagr, 4),
        "total_ret": round(total_ret, 4),
        "max_dd": round(max_dd, 4),
        "max_dd_date": max_dd_date,
        "dd_aug24": round(dd_aug, 4) if not math.isnan(dd_aug) else None,
        "calmar_ann": round(calmar_ann, 4) if not math.isnan(calmar_ann) else None,
        "avg_hold_days": round(avg_hold, 1),
        "exit_reasons": exit_reasons,
    }


def _build_equity_curve(closed: list[TradeSim], initial_balance: float,
                         position_size: float) -> pd.DataFrame:
    """Trade sequence をそのまま equity に畳み込む（同時保有を考慮しないシンプル版）。
    engine とは厳密に一致しないが trend と maxDD の相対比較には十分。"""
    if not closed:
        return pd.DataFrame({"equity": [initial_balance]},
                            index=pd.DatetimeIndex([pd.Timestamp("2021-05-19")]))
    events = []
    for s in closed:
        if s.exit_date and s.pnl_pct is not None:
            events.append((pd.Timestamp(s.exit_date), s.pnl_pct))
    if not events:
        return pd.DataFrame({"equity": [initial_balance]},
                            index=pd.DatetimeIndex([pd.Timestamp("2021-05-19")]))
    events.sort(key=lambda x: x[0])
    bal = initial_balance
    dates, balances = [], []
    dates.append(events[0][0] - pd.Timedelta(days=1))
    balances.append(bal)
    for ts, pnl in events:
        bal += position_size * pnl
        dates.append(ts)
        balances.append(bal)
    idx = pd.DatetimeIndex(dates)
    return pd.DataFrame({"equity": balances}, index=idx)


# ──────────────────────────────────────────────────────────────────────────────
# Main
# ──────────────────────────────────────────────────────────────────────────────

def main():
    print("=" * 96)
    print("2-H S1ゲート — trailing exit (目標値なし) vs fixed exit edge 検証")
    print(f"period {START}..{END}  position_size=100k  initial_balance=1M  max_concurrent=10")
    print("GUARDRAILS: annualized Sharpe/CAGR-calmar, CI, cost-ON, 2024-08 DD分離")
    print("=" * 96)

    # ── Step 1: baseline backtest (FIXED exit) ──────────────────────────────
    print("\n[1] baseline backtest (fixed exit: stop -5% / MA25回帰 / max 5d)...")
    thresholds = compute_sector_thresholds_from_cache(k=2.0)
    res_base = run_backtest(
        sector_thresholds=thresholds,
        start_date=START, end_date=END,
        max_concurrent_positions=10,
        position_size=100_000,
        initial_balance=1_000_000,
    )
    print(f"    total trades={len(res_base.trades)}  "
          f"closed={sum(1 for t in res_base.trades if t.pnl_pct is not None)}")

    # ── Step 2: load bar map for all traded codes ───────────────────────────
    traded_codes = list({t.code for t in res_base.trades})
    print(f"\n[2] loading bars for {len(traded_codes)} codes...")
    bar_map = _load_bar_map(traded_codes)
    print(f"    loaded {len(bar_map)} bar series")

    # ── Step 3: build baseline TradeSim list (from engine trades) ──────────
    # cost-OFF baseline
    baseline_sims_off: list[TradeSim] = []
    baseline_sims_on: list[TradeSim] = []
    for t in res_base.trades:
        if t.pnl_pct is None:
            continue
        baseline_sims_off.append(TradeSim(
            code=t.code, entry_date=t.entry_date, entry_price=t.entry_price,
            exit_date=t.exit_date, exit_price=t.exit_price, exit_reason=t.exit_reason,
            pnl_pct=t.pnl_pct, hold_days=t.hold_days,
        ))
        # cost-ON: recompute cost on top of engine pnl
        cost = _round_trip_cost_pct(t.entry_price, t.pnl_pct, 100_000,
                                     COST_SLIP_BPS, COST_COMMISSION)
        baseline_sims_on.append(TradeSim(
            code=t.code, entry_date=t.entry_date, entry_price=t.entry_price,
            exit_date=t.exit_date, exit_price=t.exit_price, exit_reason=t.exit_reason,
            pnl_pct=float(t.pnl_pct - cost), hold_days=t.hold_days,
        ))

    # ── Step 4: simulate trailing variants ─────────────────────────────────
    results: dict[str, dict] = {}

    # baseline cost-OFF
    print("\n[3] computing baseline stats (fixed exit, cost-OFF)...")
    stats_base_off = trade_stats(baseline_sims_off)
    results["baseline_fixed_costOFF"] = stats_base_off

    # baseline cost-ON
    print("    computing baseline stats (fixed exit, cost-ON)...")
    stats_base_on = trade_stats(baseline_sims_on)
    results["baseline_fixed_costON"] = stats_base_on

    # trailing variants cost-OFF + cost-ON
    for trail_pct, max_hold in TRAIL_VARIANTS:
        key_off = f"trail_{int(trail_pct*100)}pct_hold{max_hold}d_costOFF"
        key_on  = f"trail_{int(trail_pct*100)}pct_hold{max_hold}d_costON"
        print(f"\n[4] trail={trail_pct*100:.0f}% max_hold={max_hold}d ...")

        sims_off = [simulate_trailing(t, bar_map, trail_pct, max_hold,
                                       apply_cost=False)
                    for t in res_base.trades if t.entry_price > 0]
        sims_on  = [simulate_trailing(t, bar_map, trail_pct, max_hold,
                                       apply_cost=True)
                    for t in res_base.trades if t.entry_price > 0]

        closed_off = [s for s in sims_off if s.pnl_pct is not None]
        closed_on  = [s for s in sims_on  if s.pnl_pct is not None]
        print(f"    closed_off={len(closed_off)}  closed_on={len(closed_on)}")

        results[key_off] = trade_stats(sims_off)
        results[key_on]  = trade_stats(sims_on)

    # ── Step 5: scorecard ─────────────────────────────────────────────────
    print("\n" + "=" * 96)
    print("SCORECARD (annualized Sharpe / CAGR-calmar / maxDD / 2024-08 DD)")
    print("Trailing = same entries, post-process only exit logic. CI95 on avg_return.")
    print("=" * 96)

    col_keys = ["baseline_fixed_costOFF", "baseline_fixed_costON"] + [
        f"trail_{int(tp*100)}pct_hold{mh}d_cost{c}"
        for tp, mh in TRAIL_VARIANTS for c in ["OFF", "ON"]
    ]

    hdr = (f"{'variant':<36} {'N':>5} {'win%':>6} {'avg_ret':>8} {'ret_ci95_lo':>11} "
           f"{'ret_ci95_hi':>11} {'hold':>5} {'SharpeAnn':>9} {'CAGR':>7} "
           f"{'maxDD':>7} {'DD_aug':>7} {'calmar':>7}")
    print(hdr)
    print("-" * 96)

    for key in col_keys:
        r = results.get(key, {})
        if not r:
            print(f"{key:<36}  (no data)")
            continue
        dd_aug = r.get("dd_aug24")
        calmar = r.get("calmar_ann")
        print(
            f"{key:<36}"
            f" {r.get('n_trades', 0):>5}"
            f" {r.get('win_rate', 0)*100:>5.1f}%"
            f" {r.get('avg_return', 0)*100:>7.2f}%"
            f" {(r['ret_ci95'][0])*100:>10.2f}%"
            f" {(r['ret_ci95'][1])*100:>10.2f}%"
            f" {r.get('avg_hold_days', 0):>5.1f}"
            f" {r.get('sharpe_ann', 0):>9.3f}"
            f" {r.get('cagr', 0)*100:>6.1f}%"
            f" {r.get('max_dd', 0)*100:>6.1f}%"
            f" {dd_aug*100 if dd_aug is not None else float('nan'):>6.1f}%"
            f" {calmar if calmar is not None else float('nan'):>7.3f}"
        )

    # ── Step 6: gate verdict ───────────────────────────────────────────────
    print("\n" + "=" * 96)
    print("GATE VERDICT MATERIALS (自動結論せず材料提示)")
    print("=" * 96)
    base = results.get("baseline_fixed_costOFF", {})
    print(f"\nBASELINE (fixed, cost-OFF):")
    print(f"  N={base.get('n_trades',0)}  win_rate={base.get('win_rate',0)*100:.1f}%  "
          f"avg_ret={base.get('avg_return',0)*100:.2f}%  "
          f"Sharpe_ann={base.get('sharpe_ann',0):.3f}  "
          f"CAGR={base.get('cagr',0)*100:.1f}%  "
          f"maxDD={base.get('max_dd',0)*100:.1f}%  "
          f"calmar_ann={base.get('calmar_ann') or 'nan'}")
    print(f"  exit_reasons: {base.get('exit_reasons', {})}")

    # find best trailing variant (cost-OFF, by calmar_ann)
    trail_off_keys = [k for k in col_keys if "trail" in k and "costOFF" in k]
    best_key = max(trail_off_keys, key=lambda k: results[k].get("calmar_ann") or -9e9)
    best = results[best_key]
    print(f"\nBEST TRAILING (cost-OFF): {best_key}")
    print(f"  N={best.get('n_trades',0)}  win_rate={best.get('win_rate',0)*100:.1f}%  "
          f"avg_ret={best.get('avg_return',0)*100:.2f}%  "
          f"Sharpe_ann={best.get('sharpe_ann',0):.3f}  "
          f"CAGR={best.get('cagr',0)*100:.1f}%  "
          f"maxDD={best.get('max_dd',0)*100:.1f}%  "
          f"calmar_ann={best.get('calmar_ann') or 'nan'}")
    print(f"  exit_reasons: {best.get('exit_reasons', {})}")

    # CI overlap check
    base_ci = base.get("ret_ci95", [None, None])
    best_ci = best.get("ret_ci95", [None, None])
    if all(v is not None for v in base_ci + best_ci):
        overlap = not (base_ci[1] < best_ci[0] or best_ci[1] < base_ci[0])
        print(f"\nCI overlap (avg_return baseline vs best trailing): {overlap}")
        print(f"  baseline CI95: [{base_ci[0]*100:.2f}%, {base_ci[1]*100:.2f}%]")
        print(f"  best trail CI95: [{best_ci[0]*100:.2f}%, {best_ci[1]*100:.2f}%]")
        if overlap:
            print("  → CI OVERLAP: avg_return 差はノイズ範囲。edge 有意でない。")
        else:
            print("  → NO CI OVERLAP: avg_return に統計的差あり。")

    # calmar delta
    base_cal = base.get("calmar_ann")
    best_cal = best.get("calmar_ann")
    if base_cal and best_cal:
        delta = best_cal - base_cal
        print(f"\nCalmar delta (best_trail - baseline): {delta:+.3f}")
        pct_gain = delta / abs(base_cal) * 100 if base_cal != 0 else float("nan")
        print(f"  relative gain: {pct_gain:+.1f}%")

    # 2024-08 check
    base_aug = base.get("dd_aug24")
    best_aug = best.get("dd_aug24")
    if base_aug is not None and best_aug is not None:
        print(f"\n2024-08 maxDD: baseline={base_aug*100:.1f}%  best_trail={best_aug*100:.1f}%")
        print("  (single-crash dependency check)")

    # cost sensitivity
    base_on = results.get("baseline_fixed_costON", {})
    best_on_key = best_key.replace("costOFF", "costON")
    best_on = results.get(best_on_key, {})
    if base_on and best_on:
        print(f"\nCOST-ON sensitivity:")
        print(f"  baseline costON: Sharpe_ann={base_on.get('sharpe_ann',0):.3f}  "
              f"calmar={base_on.get('calmar_ann') or 'nan'}")
        print(f"  best_trail costON: Sharpe_ann={best_on.get('sharpe_ann',0):.3f}  "
              f"calmar={best_on.get('calmar_ann') or 'nan'}")

    # save
    Path(OUT_JSON).write_text(json.dumps(results, indent=2, default=str))
    print(f"\nsaved -> {OUT_JSON}")
    print("\nSCORECARD_JSON:", json.dumps(
        {k: {kk: round(vv, 4) if isinstance(vv, float) else vv
             for kk, vv in v.items()}
         for k, v in results.items()}, default=str))


if __name__ == "__main__":
    main()
