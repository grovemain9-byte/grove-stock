"""(iii)-a レジーム条件別 A/B サブ分析。

目的: regime_filter の edge が「どの市場レジームに・どれだけ」存在するかを
3年データ(コスト込・end固定)で実測する。年次WF(Y1/Y2/Y3)と直交する
「市場状態」軸の切り口＝edgeが少数の弱気日に集中(脆い)か弱気サブサンプル
全体で頑健かを判定する。

設計（engine不変・既存paramのみ）:
  control   = run_backtest(p4_required=False)   # G3凍結ベースラインの式
  treatment = run_backtest(p4_required=True)    # regime_filter(hypothesis_shadow
                                                #   id4 の param_delta と同一)
  バケット  = allowed_regimes で bearish / ranging / bullish / all を切替
              (engine:228-242 — bullish dev>=+3% / bearish dev<=-3% / 中間ranging。
               エントリ日のみ制御、exitは継続＝現実的)

G3凍結と同条件: consensus_min=3, max_concurrent=7, slippage10bps+手数料,
end_date=2026-05-15固定（cache成長でdriftさせない＝再現性）。
"""
from __future__ import annotations

import logging
import time

from src.backtest.runner import compute_sector_thresholds_from_cache
from src.backtest.engine import run_backtest

BASELINE_END = "2026-05-15"  # G3凍結と同一固定
BUCKETS = [
    ("all", None),
    ("bearish", {"bearish"}),
    ("ranging", {"ranging"}),
    ("bullish", {"bullish"}),
]
_KEYS = ("closed", "win_rate", "avg_return", "sharpe_per_trade", "max_drawdown")


def _run(th: dict, *, p4_required: bool, allowed_regimes) -> dict:
    """run_backtest を retry+backoff で包む。engine._load_nikkei_bars は
    キャッシュ無しで毎回 yfinance ^N225 を叩くため、連続実行でレート制限に
    当たり空df→KeyError('date') になる（engine不変＝ここで吸収）。
    """
    backoffs = [0, 15, 45, 90]  # 試行間スリープ(秒)。^N225レート窓を跨ぐ
    last_err: Exception | None = None
    for attempt, wait in enumerate(backoffs):
        if wait:
            time.sleep(wait)
        try:
            res = run_backtest(
                sector_thresholds=th,
                max_concurrent_positions=7,
                consensus_min=3,
                p4_required=p4_required,
                p4_threshold=0.0,
                end_date=BASELINE_END,
                slippage_bps=10.0,
                apply_commission=True,
                allowed_regimes=allowed_regimes,
            )
            return res.metrics()
        except Exception as e:  # noqa: BLE001 — yfinance揺らぎを跨いで再試行
            last_err = e
            print(f"  [retry {attempt+1}/{len(backoffs)}] backtest失敗: "
                  f"{type(e).__name__}: {str(e)[:80]}")
    raise RuntimeError(f"run_backtest が {len(backoffs)}回失敗: {last_err}")


def main():
    logging.disable(logging.WARNING)
    th = compute_sector_thresholds_from_cache(k=2.0)

    print(f"レジーム条件別 A/B（3年・コスト込10bps+手数料・end={BASELINE_END}固定）")
    print("treatment=p4_required(regime_filter) / control=p4_optional(G3式)")
    print("=" * 92)
    hdr = (f"{'bucket':>8} {'arm':>9} {'closed':>7} {'勝率':>7} "
           f"{'avg_ret':>9} {'Sharpe/t':>9} {'maxDD':>8}")
    for bucket, regimes in BUCKETS:
        print("-" * 92)
        print(hdr)
        rows = {}
        for arm, p4 in (("control", False), ("treatment", True)):
            raw = _run(th, p4_required=p4, allowed_regimes=regimes)
            # 取引0件のバケット(例: bullish×treatment=設計通り休止)は
            # metrics() が一部キーを欠く → 安全に0埋め
            m = {k: raw.get(k, 0.0) for k in
                 ("closed", "win_rate", "avg_return",
                  "sharpe_per_trade", "max_drawdown")}
            rows[arm] = m
            print(f"{bucket:>8} {arm:>9} {int(m['closed']):>7} "
                  f"{m['win_rate']*100:>6.1f}% {m['avg_return']*100:>+8.2f}% "
                  f"{m['sharpe_per_trade']:>9.4f} {m['max_drawdown']*100:>+7.1f}%")
        # treatment - control デルタ（edgeの所在）
        c, t = rows["control"], rows["treatment"]
        d_avg = (t["avg_return"] - c["avg_return"]) * 100
        d_shp = t["sharpe_per_trade"] - c["sharpe_per_trade"]
        d_dd = (t["max_drawdown"] - c["max_drawdown"]) * 100
        print(f"{bucket:>8} {'Δ(t-c)':>9} {t['closed']-c['closed']:>+7} "
              f"{'':>7} {d_avg:>+8.2f}% {d_shp:>+9.4f} {d_dd:>+7.1f}%")
    print("=" * 92)
    print("※ Δ avg_ret>0 & ΔSharpe>0 & ΔmaxDD>0(浅く) = そのレジームでtreatment優位。")
    print("※ bullishでtreatment closed≈0 は設計通り（弱気のみ建玉）＝機会損失の定量。")


if __name__ == "__main__":
    main()
