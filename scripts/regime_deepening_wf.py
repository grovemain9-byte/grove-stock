"""§9-1 regime深掘り: regime_filter の p4_threshold を1段締めて床が締まるか
WF3窓で検証（事前登録ゲート・結果を見て門を動かさない）。

機構（本日 regime_conditional_ab.py で実測・blueprint §0.5 P2/§2-i）:
逆張りの alpha は弱気バケットに集中（bearish dev≤-3% で avg+2.62%）、
強気は負け、レンジほぼ0。regime_filter(p4_required=True, p4_threshold=0.0)は
「日経<MA25」で建玉＝mild弱気も拾う。仮説: 閾値を**負側へ締める**と
高alphaな深い弱気へ集中し床(リスク調整後)が締まるのでは。

=== 事前登録ゲート（結果取得"前"に固定・rank_validate教訓＝門を先に宣言）===
- baseline = regime_filter: G3 ∪ {p4_required:True, p4_threshold:0.0}
- 候補 = baseline の **p4_threshold のみ** ∈ {-0.01, -0.02, -0.03}
  （小さい事前宣言集合のみ・sweep/最良値拾い=curve-fit は禁止）
- 窓 = hypothesis_loop と同一 Y1/Y2/Y3、cost = slippage10bps+手数料、
  end固定、pass基準も同一: その窓で Sharpe↑ かつ maxDD非悪化(eps 0.005)
- **GO = 3/3窓 のみ**。1-2窓 = PASS（レジーム依存→採用しない）。
- GO が出ても適用は discipline_apply 経由＋S1 GO＋Grove承認（本script は計測のみ）。
- trace-before-alarm: 「締まった」と断定する前に各窓の数値を必ず提示。
  未trace/2窓以下は『PASS（仮説止まり）』と明示。
"""
from __future__ import annotations

import logging
import time

from src.backtest.runner import compute_sector_thresholds_from_cache
from src.backtest.engine import run_backtest

BASELINE_END = "2026-05-15"   # G3凍結と同一固定（drift防止）
WINDOWS = [
    ("2023-05-01", "2024-04-30", "Y1"),
    ("2024-05-01", "2025-04-30", "Y2"),
    ("2025-05-01", "2026-05-15", "Y3"),
]
# regime_filter baseline（G3 ∪ regime_filter delta）
BASE = dict(consensus_min=3, stop_loss=-0.07, p4_required=True, p4_threshold=0.0)
CANDIDATE_THRESHOLDS = [-0.01, -0.02, -0.03]   # 事前宣言・これ以外試さない
_MAXDD_EPS = 0.005


def _metrics(th, start, end, params):
    # engine._load_nikkei_bars はキャッシュ無しで毎回 yfinance ^N225 を叩く。
    # 多数回連続でレート制限→空df→KeyError。retry+backoffで吸収
    # （graceful-degradation・regime_conditional_ab.py と同型）。
    backoffs = [0, 15, 45, 90]
    last_err: Exception | None = None
    for attempt, wait in enumerate(backoffs):
        if wait:
            time.sleep(wait)
        try:
            m = run_backtest(
                sector_thresholds=th, max_concurrent_positions=7,
                start_date=start, end_date=end, slippage_bps=10.0,
                apply_commission=True, consensus_min=params["consensus_min"],
                stop_loss=params["stop_loss"],
                p4_required=params["p4_required"],
                p4_threshold=params["p4_threshold"],
            ).metrics()
            break
        except Exception as e:  # noqa: BLE001 — yfinance揺らぎを跨ぐ
            last_err = e
            print(f"  [retry {attempt+1}/{len(backoffs)}] "
                  f"{type(e).__name__}: {str(e)[:70]}")
    else:
        raise RuntimeError(f"run_backtest {len(backoffs)}回失敗: {last_err}")
    if m.get("closed", 0) == 0:
        return None
    return {"closed": m["closed"], "sharpe": m.get("sharpe_per_trade", 0.0),
            "maxdd": m["max_drawdown"], "avg": m["avg_return"],
            "win": m["win_rate"]}


def main():
    logging.disable(logging.WARNING)
    th = compute_sector_thresholds_from_cache(k=2.0)

    base_by_w = {tag: _metrics(th, s, e, BASE) for s, e, tag in WINDOWS}

    print("§9-1 regime深掘り WF（baseline=regime_filter p4_thr=0.0・cost込・"
          f"end={BASELINE_END}固定）")
    print("事前登録ゲート: 3/3窓でSharpe↑&maxDD非悪化 のみGO / 1-2窓=PASS")
    print("=" * 86)
    for thr in CANDIDATE_THRESHOLDS:
        cand = {**BASE, "p4_threshold": thr}
        npass = 0
        print(f"\n[p4_threshold={thr:+.2f}]  ({'  '.join(t for _,_,t in WINDOWS)})")
        for s, e, tag in WINDOWS:
            b = base_by_w[tag]
            c = _metrics(th, s, e, cand)
            if b is None or c is None:
                print(f"  {tag}: no_data（baseline/cand 取引0）→ fail")
                continue
            passed = (c["sharpe"] > b["sharpe"]
                      and c["maxdd"] >= b["maxdd"] - _MAXDD_EPS)
            npass += int(passed)
            mk = "★pass" if passed else " fail"
            print(f"  {tag}{mk}: base Sh{b['sharpe']:+.3f}/DD{b['maxdd']*100:5.1f}%"
                  f"/決着{b['closed']} → cand Sh{c['sharpe']:+.3f}"
                  f"/DD{c['maxdd']*100:5.1f}%/決着{c['closed']}")
        verdict = "GO" if npass == len(WINDOWS) else "PASS"
        print(f"  → {npass}/{len(WINDOWS)}窓 = **{verdict}**"
              + ("" if verdict == "GO" else "（レジーム依存・採用しない）"))
    print("=" * 86)
    print("※GOでも適用は discipline_apply 経由＋S1 GO＋Grove承認（本script計測のみ）")
    print("※決着数が締めるほど減るのは設計通り（高alpha弱気へ集中）。"
          "薄silver bulletに見えてもwf 3/3 を満たさねば不採用＝rank_validate教訓")


if __name__ == "__main__":
    main()
