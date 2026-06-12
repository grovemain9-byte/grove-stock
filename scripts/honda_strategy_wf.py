"""本田戦略 WF検証ハーネス（承認済・standalone・engine不変・日足・Shadow）。

本田くん確定スペック（2026-05-19 MTG）:
  レジーム: EMA900 slope>0（EMA900[t] > EMA900[t-SLOPE_N]）
  エントリ: EMA21乖離 = (close-EMA21)/EMA21 ≤ -entry_thr  かつ レジームOK
  利確   : EMA21乖離 ≥ +exit_thr （"多め"＝MA回帰では出ない）
  損切   : EMA21乖離 ≤ -stop_thr （stop_thr>entry_thr＝下方乖離が拡大＝撤退）
  ＝EMA21非対称バンド × EMA900上昇ゲート。**もはやBNF式(平均回帰)でない**＝
  本セッション検証のG3/regime機構は一切転移しない＝ゼロから自前ゲート。

=== 事前登録ゲート（結果取得"前"に固定・rank_validate/§9-1/Bailey教訓）===
- 窓 = hypothesis_loop同一 Y1/Y2/Y3・cost=slippage10bps+立花手数料往復・
  end=2026-05-15固定。新戦略ゆえ"対基準"でなく**絶対基準**:
  ある config が **3/3窓で sharpe_per_trade>0 かつ maxDD>-0.35** のみ "ROBUST"。
- グリッドは少数を原理で事前宣言（最良拾い禁止＝選択バイアス）:
  EMA21/EMA900/SLOPE_N=20 は本田spec/原理で固定（tuneしない）。
  entry_thr∈{0.01,0.02} exit_thr∈{0.03,0.05} stop_thr∈{0.03,0.05} =8通り。
- 判定の読み: 「最良を選ぶ」でなく「**原理configのどれかが3/3頑健か**」の
  characterization。ROBUSTが出ても採用でなく次段の事前登録confirmへ。
  Bailey: 8config×3窓は選択バイアス＝3/3は必要条件であって十分でない。
- **EMA900履歴十分性を一級市民として検査・報告**（3年cacheで~3.6年EMA900は
  warmup不足＝黙って半端値を使えば self-deception。足りない分は正直に欠測扱い
  ＆窓ごと有効銘柄数を必ず出す）。trace-before-alarm: 数字を出してから断定。
- 適用は一切しない（Shadow）。本scriptは計測のみ。
"""
from __future__ import annotations

import logging

import numpy as np
import pandas as pd

from src.data.cache import load_universe, load_bars
from src.data.db import calc_commission

BASELINE_END = "2026-05-15"
WINDOWS = [
    ("2023-05-01", "2024-04-30", "Y1"),
    ("2024-05-01", "2025-04-30", "Y2"),
    ("2025-05-01", "2026-05-15", "Y3"),
]
EMA_FAST, EMA_SLOW, SLOPE_N = 21, 900, 20      # 本田spec/原理固定・tuneしない
GRID = [(e, x, s) for e in (0.01, 0.02)
        for x in (0.03, 0.05) for s in (0.03, 0.05)]
SLIPPAGE = 0.0010                               # 片道10bps（G3規律と同一）
MAXDD_FLOOR = -0.35                             # 事前固定の破滅DD境界


def _prep(code: str, end: str) -> pd.DataFrame | None:
    """endまで全history取得しEMA21/EMA900/dev/slope付与（warmupに全期間使う）。"""
    df = load_bars(code, end=end)
    if df is None or len(df) < EMA_SLOW + SLOPE_N + 5:
        return None                              # EMA900成立に履歴不足＝除外
    df = df.sort_values("date").reset_index(drop=True)
    c = df["close"].astype(float)
    df["ema_f"] = c.ewm(span=EMA_FAST, adjust=False).mean()
    df["ema_s"] = c.ewm(span=EMA_SLOW, adjust=False).mean()
    df["dev"] = (c - df["ema_f"]) / df["ema_f"]
    df["slope_up"] = df["ema_s"] > df["ema_s"].shift(SLOPE_N)
    return df


def _trades(df: pd.DataFrame, start: str, end: str,
            entry_thr: float, exit_thr: float, stop_thr: float) -> list[float]:
    """単一保有/銘柄・イベント駆動。窓内エントリのみ。net pnl%（cost込）を返す。"""
    d = pd.to_datetime(df["date"])  # datetime64 に統一して比較（型不一致回避）
    w = df[(d >= pd.Timestamp(start)) & (d <= pd.Timestamp(end))]
    rets, pos_entry = [], None
    for _, r in w.iterrows():
        dev = r["dev"]
        if pos_entry is None:
            if bool(r["slope_up"]) and dev <= -entry_thr:
                pos_entry = float(r["close"])    # 当日終値約定（engine流儀）
        else:
            if dev >= exit_thr or dev <= -stop_thr:
                px_in, px_out = pos_entry, float(r["close"])
                gross = (px_out - px_in) / px_in
                # 往復: slippage両側 + 立花手数料往復（10万円相当の名目で近似）
                sh = max(int(100_000 / px_in / 100) * 100, 100)
                comm = (calc_commission(px_in, sh)
                        + calc_commission(px_out, sh)) / (px_in * sh)
                rets.append(gross - 2 * SLIPPAGE - comm)
                pos_entry = None
    return rets


def _metrics(rets: list[float]) -> dict | None:
    if len(rets) < 5:
        return None
    a = np.array(rets)
    eq = np.cumprod(1 + a)
    peak = np.maximum.accumulate(eq)
    mdd = float((eq / peak - 1).min())
    sd = a.std(ddof=1)
    return {"closed": len(a), "sharpe": float(a.mean() / sd) if sd > 0 else 0.0,
            "avg": float(a.mean()), "win": float((a > 0).mean()), "maxdd": mdd}


def main():
    logging.disable(logging.WARNING)
    universe = list(load_universe()["Code"])
    print(f"本田戦略 WF（standalone・日足・cost込・end={BASELINE_END}固定）"
          f" universe={len(universe)} grid={len(GRID)}")
    print("スペック: EMA21乖離バンド × EMA900上昇ゲート（非BNF・自前絶対基準）")
    print("=" * 90)

    # 銘柄ごとに一度だけ指標計算（窓×config間で再利用）
    prepped, n_insufficient = {}, 0
    for code in universe:
        d = _prep(code, BASELINE_END)
        if d is None:
            n_insufficient += 1
        else:
            prepped[code] = d
    print(f"EMA900履歴十分性: 有効 {len(prepped)} / 不足除外 {n_insufficient}"
          f" / universe {len(universe)}")
    if not prepped:
        print("→ 全銘柄でEMA900履歴不足。3年cacheでは本田spec(EMA900~3.6年)を"
              "忠実検証不能＝データ拡張(J-Quants Light 5年)が前提条件。No-test。")
        return

    for (et, xt, st) in GRID:
        npass, line = 0, []
        for s, e, tag in WINDOWS:
            allr: list[float] = []
            for d in prepped.values():
                allr += _trades(d, s, e, et, xt, st)
            m = _metrics(allr)
            if m is None:
                line.append(f"{tag}:no_data")
                continue
            ok = m["sharpe"] > 0 and m["maxdd"] > MAXDD_FLOOR
            npass += int(ok)
            line.append(f"{tag}{'★' if ok else ' '}Sh{m['sharpe']:+.3f}"
                        f"/DD{m['maxdd']*100:5.1f}%/n{m['closed']}")
        verd = "ROBUST(3/3)" if npass == 3 else f"PASS({npass}/3)"
        print(f"e{et:.2f}/x{xt:.2f}/s{st:.2f}: " + "  ".join(line)
              + f"  → {verd}")
    print("=" * 90)
    print("※ROBUSTでも採用でなく次段の事前登録confirmへ（Bailey: 8config選択"
          "バイアス＝3/3は必要条件）。Shadowのみ・適用なし。")
    print("※EMA900不足除外が多い場合＝3年cacheの限界。data拡張要否を別途判断。")


if __name__ == "__main__":
    main()
