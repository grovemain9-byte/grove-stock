"""F+G audit (2026-05-20): BNF base calibration shrinkage + Honda bridge regime artifact discrimination.

実装根拠 (gemini deep_research + Plan agent 敵対的レビュー が独立収束):
  F  = BNF G3 base が cost-aware で edge を持つか統計的検定
       - bootstrap Sharpe CI (10000 resample, percentile)
       - Probabilistic Sharpe Ratio (PSR)
       - Deflated Sharpe Ratio (DSR, Lopez de Prado, k=6 trials adjustment)
       - consensus-stratified mean return (信号強度の calibration)

  G  = Honda 3候補 (extended_hold/asymmetric_tp/ema900_uptrend) を
       Nikkei regime (bullish/ranging/bearish, ab_preregistration.md §1 定義) で stratify
       - H_signal: 全 regime で uniform 改善 → 実シグナル
       - H_regime: bullish のみ改善・他 ≈0/負 → regime artifact (= Honda bridge死亡)

新 backtest はせず、既存 engine を再呼び出し → trades 抽出 → 後処理のみ。
"""
from __future__ import annotations

import json
import logging
import math
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd
from scipy.stats import norm

from src.backtest.engine import _load_nikkei_bars, run_backtest
from src.backtest.runner import compute_sector_thresholds_from_cache

WINDOWS = [
    ("2023-05-01", "2024-04-30", "Y1"),
    ("2024-05-01", "2025-04-30", "Y2"),
    ("2025-05-01", "2026-05-15", "Y3"),
]

# k=6 = hypothesis_loop registry の現件数 (regime_filter, stop_tight_5, consensus_4,
# extended_hold, asymmetric_tp, ema900_uptrend)。DSR 多重検定補正に使用。
N_HYPOTHESES_TESTED = 6

G3_PARAMS = dict(
    consensus_min=3, stop_loss=-0.07,
    p4_required=False, p4_threshold=0.0,
    max_hold_days=15, take_profit_dev=0.0,
    per_stock_uptrend_required=False,
)

# G3 + 3 Honda 候補。regime_filter は既知 GO/3/3 で F+G の論点ではないため除外。
CANDIDATES = {
    "G3":              {},
    "extended_hold":   {"max_hold_days": 25},
    "asymmetric_tp":   {"take_profit_dev": 0.02},
    "ema900_uptrend":  {"per_stock_uptrend_required": True},
}


# ---------- 1. CAPTURE: trades + regime ----------

def classify_regime(nk_dev: float | None) -> str:
    """ab_preregistration.md §1 定義: dev>=+3% bullish / dev<=-3% bearish / else ranging."""
    if nk_dev is None or (isinstance(nk_dev, float) and math.isnan(nk_dev)):
        return "unknown"
    if nk_dev >= 0.03:
        return "bullish"
    if nk_dev <= -0.03:
        return "bearish"
    return "ranging"


def run_capture() -> pd.DataFrame:
    nikkei = _load_nikkei_bars()
    nikkei["date"] = pd.to_datetime(nikkei["date"])
    nk_by_date = dict(zip(nikkei["date"], nikkei["nikkei_dev"]))
    th = compute_sector_thresholds_from_cache(k=2.0)
    rows = []
    for s, e, tag in WINDOWS:
        for name, delta in CANDIDATES.items():
            params = {**G3_PARAMS, **delta}
            try:
                res = run_backtest(
                    sector_thresholds=th, max_concurrent_positions=7,
                    start_date=s, end_date=e,
                    slippage_bps=10.0, apply_commission=True,
                    **params,
                )
            except RuntimeError as ex:
                if str(ex).startswith("insufficient_warmup"):
                    print(f"  [{name}/{tag}] insufficient_warmup → skip")
                    continue
                raise
            n_closed = 0
            for t in res.trades:
                if t.pnl_pct is None:
                    continue
                ed_ts = pd.Timestamp(t.entry_date)
                nk = nk_by_date.get(ed_ts)
                rows.append({
                    "candidate": name,
                    "window": tag,
                    "code": t.code,
                    "entry_date": str(t.entry_date),
                    "exit_date": str(t.exit_date),
                    "pnl_pct": float(t.pnl_pct),
                    "hold_days": int(t.hold_days or 0),
                    "consensus": int(t.consensus_at_entry),
                    "exit_reason": str(t.exit_reason),
                    "nikkei_dev_entry": float(nk) if nk is not None and not (isinstance(nk, float) and math.isnan(nk)) else None,
                    "regime": classify_regime(nk),
                })
                n_closed += 1
            print(f"  [{name}/{tag}] {n_closed} closed trades captured")
    return pd.DataFrame(rows)


# ---------- 2. F: BNF base 統計的有意性 ----------

def bootstrap_sharpe_ci(returns: np.ndarray, n_boot: int = 10000,
                         ci: float = 0.95, seed: int = 42) -> tuple[float, float]:
    rng = np.random.default_rng(seed)
    n = len(returns)
    sharpes = np.empty(n_boot)
    for i in range(n_boot):
        sample = rng.choice(returns, size=n, replace=True)
        s = sample.std(ddof=1)
        sharpes[i] = sample.mean() / s if s > 0 else 0.0
    lo = float(np.percentile(sharpes, (1 - ci) / 2 * 100))
    hi = float(np.percentile(sharpes, (1 + ci) / 2 * 100))
    return lo, hi


def deflated_sharpe(sr: float, n_trades: int, n_trials: int) -> dict:
    """Lopez de Prado: PSR + DSR (k試行多重検定補正).

    SE(SR) ≈ √((1 + 0.5·SR²) / (T - 1))   (Mertens 2002, normal approx)
    PSR = Φ((SR - 0) / SE(SR))
    SR*  = √Var(SR) · ((1-γ)·Φ⁻¹(1 - 1/k) + γ·Φ⁻¹(1 - 1/(k·e)))   (expected max of k IID)
    DSR = Φ((SR - SR*) / SE(SR))
    """
    gamma = 0.5772156649  # Euler-Mascheroni
    if n_trades < 2:
        return {"psr": 0.5, "dsr": 0.5, "se_sr": float("nan"), "sr_threshold": float("nan")}
    se_sr = math.sqrt((1 - sr + 0.5 * sr * sr) / (n_trades - 1))
    psr = norm.cdf(sr / se_sr) if se_sr > 0 else 0.5
    if n_trials > 1:
        inv_n = norm.ppf(1.0 - 1.0 / n_trials)
        inv_ne = norm.ppf(1.0 - 1.0 / (n_trials * math.e))
        sr_threshold = se_sr * ((1.0 - gamma) * inv_n + gamma * inv_ne)
    else:
        sr_threshold = 0.0
    dsr = norm.cdf((sr - sr_threshold) / se_sr) if se_sr > 0 else 0.5
    return {
        "psr": float(psr),
        "dsr": float(dsr),
        "se_sr": float(se_sr),
        "sr_threshold_for_k_trials": float(sr_threshold),
    }


def audit_f(df: pd.DataFrame) -> dict:
    g3 = df[df["candidate"] == "G3"]
    rets = g3["pnl_pct"].values.astype(float)
    if len(rets) < 10:
        return {"err": "insufficient_g3_trades", "n": int(len(rets))}
    n = len(rets)
    mean = float(rets.mean())
    std = float(rets.std(ddof=1))
    sr = mean / std if std > 0 else 0.0
    ci_lo, ci_hi = bootstrap_sharpe_ci(rets)
    dsr_dict = deflated_sharpe(sr, n, n_trials=N_HYPOTHESES_TESTED)
    # consensus stratification (signal-strength calibration)
    by_cons = (
        g3.groupby("consensus")
          .agg(n=("pnl_pct", "size"),
               mean=("pnl_pct", "mean"),
               win=("pnl_pct", lambda x: float((x > 0).mean())),
               sharpe=("pnl_pct", lambda x: float(x.mean() / x.std(ddof=1)) if x.std(ddof=1) > 0 else 0.0))
          .reset_index().to_dict("records")
    )
    # regime stratification on G3 itself (for context)
    by_regime = (
        g3.groupby("regime")
          .agg(n=("pnl_pct", "size"),
               mean=("pnl_pct", "mean"),
               win=("pnl_pct", lambda x: float((x > 0).mean())))
          .reset_index().to_dict("records")
    )
    # killer verdict
    edge_survives = ci_lo > 0
    psr_significant = dsr_dict["psr"] > 0.95
    dsr_significant = dsr_dict["dsr"] > 0.95
    return {
        "n_trades": int(n),
        "mean_per_trade": mean,
        "std_per_trade": std,
        "sharpe_per_trade": float(sr),
        "sharpe_CI_95_bootstrap": [ci_lo, ci_hi],
        "psr": dsr_dict["psr"],
        "dsr": dsr_dict["dsr"],
        "se_sr": dsr_dict["se_sr"],
        "sr_threshold_for_k_trials": dsr_dict["sr_threshold_for_k_trials"],
        "n_trials_for_dsr": N_HYPOTHESES_TESTED,
        "edge_survives_bootstrap_CI": bool(edge_survives),
        "edge_significant_PSR_95": bool(psr_significant),
        "edge_significant_DSR_95": bool(dsr_significant),
        "verdict": (
            "EDGE_SURVIVES" if (edge_survives and dsr_significant)
            else "MARGINAL" if (edge_survives or psr_significant)
            else "EDGE_NULL"
        ),
        "consensus_breakdown": by_cons,
        "regime_breakdown_g3": by_regime,
    }


# ---------- 3. G: Honda bridge regime-stratified Δ ----------

def audit_g(df: pd.DataFrame) -> dict:
    g3 = df[df["candidate"] == "G3"]
    result = {}
    for name in ("extended_hold", "asymmetric_tp", "ema900_uptrend"):
        cand = df[df["candidate"] == name]
        if len(cand) == 0:
            result[name] = {"err": "no_trades_captured", "verdict": "no_data"}
            continue
        rows = []
        for regime in ("bullish", "ranging", "bearish"):
            cr = cand[cand["regime"] == regime]["pnl_pct"].values.astype(float)
            br = g3[g3["regime"] == regime]["pnl_pct"].values.astype(float)
            if len(cr) < 5 or len(br) < 5:
                rows.append({"regime": regime, "n_cand": int(len(cr)), "n_base": int(len(br)), "skip": "insufficient_sample"})
                continue
            rows.append({
                "regime": regime,
                "n_cand": int(len(cr)),
                "n_base": int(len(br)),
                "cand_mean": float(cr.mean()),
                "base_mean": float(br.mean()),
                "delta_mean": float(cr.mean() - br.mean()),
                "cand_win": float((cr > 0).mean()),
                "base_win": float((br > 0).mean()),
                "delta_win": float((cr > 0).mean() - (br > 0).mean()),
            })
        # Pattern verdict
        valid = [r for r in rows if "delta_mean" in r]
        if len(valid) == 0:
            verdict = "no_data"
        else:
            pos = [r for r in valid if r["delta_mean"] > 0]
            neg = [r for r in valid if r["delta_mean"] <= 0]
            if len(pos) == len(valid):
                verdict = "H_signal_uniform_improvement"
            elif len(pos) == 1 and any(r["regime"] == "bullish" and r["delta_mean"] > 0 for r in pos):
                verdict = "H_regime_bullish_only_artifact"
            elif len(neg) == len(valid):
                verdict = "H_anti_signal_uniform_degradation"
            else:
                verdict = "H_mixed_regime_dependent"
        result[name] = {"verdict": verdict, "regime_breakdown": rows}
    return result


# ---------- 4. MAIN ----------

def main():
    logging.disable(logging.WARNING)
    print("=== F+G AUDIT START ===")
    print(f"start_ts: {datetime.now().isoformat(timespec='seconds')}")
    print(f"candidates: {list(CANDIDATES.keys())}  windows: {[w[2] for w in WINDOWS]}\n")
    print("-- CAPTURE --")
    df = run_capture()
    print(f"\nTotal closed trades captured: {len(df)}")
    if len(df) > 0:
        pivot = df.pivot_table(values="pnl_pct", index="candidate", columns="window",
                                aggfunc="count", fill_value=0)
        print("\nTrade count by (candidate × window):\n", pivot)

    out_dir = Path("data/fg_audit")
    out_dir.mkdir(parents=True, exist_ok=True)
    df.to_csv(out_dir / "trades.csv", index=False)
    print(f"\nTrades CSV: {out_dir / 'trades.csv'}")

    print("\n-- F: BNF base G3 calibration --")
    f_result = audit_f(df)
    print(json.dumps(f_result, indent=2, default=str, ensure_ascii=False))

    print("\n-- G: Honda bridge regime-stratified Δ --")
    g_result = audit_g(df)
    print(json.dumps(g_result, indent=2, default=str, ensure_ascii=False))

    combined = {
        "timestamp": datetime.now().isoformat(timespec="seconds"),
        "F_base_audit": f_result,
        "G_regime_stratified": g_result,
    }
    (out_dir / "audit_result.json").write_text(
        json.dumps(combined, indent=2, default=str, ensure_ascii=False))

    # Verdict summary
    print("\n=== AUDIT VERDICTS ===")
    print(f"[F] BNF base: {f_result.get('verdict', 'N/A')}")
    print(f"    sharpe CI95 bootstrap: {f_result.get('sharpe_CI_95_bootstrap')}")
    print(f"    PSR: {f_result.get('psr', 0):.4f} (need > 0.95)")
    print(f"    DSR (k={N_HYPOTHESES_TESTED}): {f_result.get('dsr', 0):.4f} (need > 0.95)")
    for name, r in g_result.items():
        print(f"[G] {name}: {r.get('verdict', 'N/A')}")
    print(f"\nResult JSON: {out_dir / 'audit_result.json'}")
    print(f"end_ts: {datetime.now().isoformat(timespec='seconds')}")


if __name__ == "__main__":
    main()
