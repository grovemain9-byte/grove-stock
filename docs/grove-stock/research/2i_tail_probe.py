"""2-I S1ゲート co-plan接地: 弱気逆張りトレードの左尾を既存データで読む（READ-ONLY）。

目的: 「弱気で厚張り」の前に、67%勝率の裏に致命的左尾が隠れてないかを既存
/tmp/2b_rev.json(Phase A生成, 210 bearishトレード)で cheap-pass 確認する。
本番src/config/は一切変更しない。設計判断のための材料出し。

3つの問い:
  Q1 左尾の深さ: 最悪トレード / 5%tile / CVaR5・CVaR10（平均の裏の最悪ケース）
  Q2 stop突き抜け(gap risk): exit_reason=stop_loss のpnl分布。-5%でクリーンに止まるか、
     ギャップで突き抜けるか（突き抜け=実データにtail実在の証拠）
  Q3 相関(集中): 最悪損失が同じ暴落窓に固まってるか（月別クラスタ）。
     固まる=「10ポジ同時被弾」リスクが実在 → sizing上げると致命化
"""
from __future__ import annotations
import json
import numpy as np
import pandas as pd
from src.backtest.regime import classify_daily_regimes, label_trades_by_regime

TRADES_JSON = "/tmp/2b_rev.json"

def cvar(pnls: np.ndarray, q: float) -> float:
    """下側q分位の expected shortfall（最悪q%の平均リターン）。"""
    if len(pnls) == 0:
        return float("nan")
    thr = np.percentile(pnls, q * 100)
    tail = pnls[pnls <= thr]
    return float(tail.mean()) if len(tail) else float("nan")

def main() -> None:
    regimes = classify_daily_regimes()
    tdf = label_trades_by_regime(TRADES_JSON, regimes)
    tdf["exit"] = pd.to_datetime(tdf["exit"])
    print("=" * 78)
    print("2-I S1 tail probe — 弱気逆張りの左尾（READ-ONLY, 既存データ）")
    print("=" * 78)

    for reg in ["bearish", "ranging", "bullish"]:
        grp = tdf[tdf["regime"] == reg]
        if len(grp) == 0:
            continue
        pnls = grp["pnl_pct"].to_numpy(dtype=float)
        n = len(pnls)
        wr = float((pnls > 0).mean())
        print(f"\n### {reg}  N={n}  win={wr*100:.1f}%  avg={pnls.mean()*100:+.2f}%")
        # Q1 左尾
        print(f"  [Q1左尾] worst={pnls.min()*100:+.1f}%  "
              f"p5={np.percentile(pnls,5)*100:+.1f}%  "
              f"CVaR5={cvar(pnls,0.05)*100:+.1f}%  CVaR10={cvar(pnls,0.10)*100:+.1f}%  "
              f"std={pnls.std(ddof=1)*100:.1f}%")
        # Q2 stop突き抜け
        if "reason" in grp.columns:
            stops = grp[grp["reason"] == "stop_loss"]["pnl_pct"].to_numpy(dtype=float)
            if len(stops):
                thru = (stops < -0.05 - 1e-9).sum()
                print(f"  [Q2 stop] n_stop={len(stops)}  "
                      f"min={stops.min()*100:+.1f}%  mean={stops.mean()*100:+.1f}%  "
                      f"突抜(<-5%)={thru} ({thru/len(stops)*100:.0f}%)")
        # Q3 相関(最悪10件の月別集中)
        worst = grp.nsmallest(min(10, n), "pnl_pct")
        months = worst["exit"].dt.to_period("M").value_counts()
        top_mon = months.head(3).to_dict()
        print(f"  [Q3相関] 最悪{len(worst)}件の exit月 集中: "
              + ", ".join(f"{str(k)}:{v}件" for k, v in top_mon.items()))

    # bearish の sizing-up naive試算（per-tradeレベル、相関無視の楽観上限）
    bear = tdf[tdf["regime"] == "bearish"]["pnl_pct"].to_numpy(dtype=float)
    print("\n" + "=" * 78)
    print("naive sizing-up (per-trade, 相関無視=楽観上限): bearishのみ size×k")
    print("  ※真のtailは相関+concurrency+gapで悪化する。これは下限見積もり")
    print("=" * 78)
    for k in [1.0, 1.5, 2.0]:
        scaled = bear * k
        print(f"  k={k}: avg={scaled.mean()*100:+.2f}%  worst={scaled.min()*100:+.1f}%  "
              f"CVaR5={cvar(scaled,0.05)*100:+.1f}%")

if __name__ == "__main__":
    main()
