"""2-B edge検証 Phase A・Track2: 順張り(モメンタム/ブレイクアウト) × regime別成績。

READ-ONLY研究script。本番 src/・config/ は変更しない（regime.py/cache.py を import するのみ）。

================================================================================
問い: 順張りは bullish(上昇トレンド)相場で、逆張りが負ける所で勝つか？
  ← 2-B「逆張りはbearish/rangingに偏る」の もう片側(対称仮説)を first-pass で測る。
================================================================================

採用した順張りロジック（素直な定義・1つ・overfit禁止＝閾値を恣意調整しない）:
  ENTRY (両条件AND):
    (1) Donchianブレイクアウト: 当日close > 過去20営業日(当日除く)の高値
    (2) トレンド整合: MA25 > MA25[20営業日前]（MA25が上向き）
  EXIT (いずれか・優先順位この順):
    (a) trailing_stop : entry以降の最高値(close基準)から -8% 下落
    (b) trend_break   : close < MA25（トレンド転換）
    (c) max_hold      : 保有 60営業日到達（順張りはトレンドに乗るため逆張りの5日より長窓）
  ※閾値(20/25/8%/60d)は順張りの教科書的既定値。最良値探索(curve-fit)はしない＝公平proxy。

逆張り(engine.py)との極性対比:
  逆張り entry = 下落で買い(乖離≤閾値) / exit = MA25回帰(上昇)で利確
  順張り entry = 上昇(高値ブレイク)で買い / exit = MA25割れ(下落)で損切り  ← 逆極性

Track1との公平比較（同一に揃えた条件）:
  - universe       : load_universe()（Track1と同一の全銘柄）
  - 期間            : cache全期間 → end=2026-05-15固定（regime_conditional_ab.py と同一・drift防止）
  - position仮定    : 等サイズ・max_concurrent=7（regime_conditional_ab.py と同一）
  - regimeラベル    : classify_daily_regimes()（src/backtest/regime.py・別定義にしない）
                      entry_date時点のregimeを採用（Track1 label_trades_by_regime と同じ作法）
  - コスト          : OFF（slippage/手数料なし）＝ engine golden 既定と同じ無コスト基準で対称比較

出力: regime × {N, win_rate±95%CI, avg_return±95%CI, sharpe}。小N regime明示。

再現コマンド:
  cd /Users/ryu/grove-stock && .venv/bin/python docs/grove-stock/research/momentum_by_regime.py
"""
from __future__ import annotations

import logging
import math
from datetime import date

import numpy as np
import pandas as pd

from src.data.cache import load_universe, load_bars
from src.backtest.regime import classify_daily_regimes, regime_summary

# ---- Track1整合パラメータ（公平比較のため固定） --------------------------------
BASELINE_END = "2026-05-15"      # regime_conditional_ab.py / regime_deepening_wf.py と同一固定
MAX_CONCURRENT = 7               # regime_conditional_ab.py と同一
POSITION_SIZE = 100_000.0        # engine 既定（等サイズ）

# ---- 順張りロジック閾値（教科書既定・恣意調整しない） ----------------------------
BREAKOUT_LOOKBACK = 20           # Donchian: 過去20営業日高値ブレイク
MA_PERIOD = 25                   # MA25（戦略制約と同じ日足MA25）
MA_SLOPE_LOOKBACK = 20           # MA25が20営業日前より上 = 上向き
TRAIL_STOP = -0.08               # entry以降高値からの trailing stop（-8%）
MAX_HOLD_DAYS = 60               # 順張りの max_hold（トレンド追従のため長め）
MIN_BARS = MA_PERIOD + MA_SLOPE_LOOKBACK + 5  # 指標warmupに必要な最小本数

logger = logging.getLogger("momentum_by_regime")


def _compute_indicators(df: pd.DataFrame) -> pd.DataFrame:
    """順張り用指標: MA25, MA25上向き, 過去20日高値(当日除く)。
    df columns: date, open, high, low, close, volume, value
    """
    d = df.sort_values("date").reset_index(drop=True).copy()
    d["ma25"] = d["close"].rolling(MA_PERIOD).mean()
    d["ma25_up"] = d["ma25"] > d["ma25"].shift(MA_SLOPE_LOOKBACK)
    # 過去20営業日(当日を含めない)の高値: closeベースのDonchian上限。
    # shift(1)で当日を除外 → 「当日closeが直近20日の高値を更新」を判定。
    d["hh20_prev"] = d["close"].rolling(BREAKOUT_LOOKBACK).max().shift(1)
    return d


def simulate_momentum() -> pd.DataFrame:
    """universe全銘柄を順張りでシミュレートし trade list を返す。

    Portfolio-level: max_concurrent枠・等サイズ。同日に複数ブレイクアウトが
    出た時は辞書順で枠を埋める（engine の従来挙動と同型＝恣意的な銘柄選別をしない）。
    Returns: tdf with columns regime_summary が要求する列
             [code, entry, exit, pnl_pct, reason, hold_days, consensus, regime]
    """
    universe = load_universe()
    if universe.empty:
        raise RuntimeError("universe empty. run cache build first.")

    # 各銘柄の指標を事前計算（end固定でreproducibility確保）
    bar_cache: dict[str, pd.DataFrame] = {}
    for _, r in universe.iterrows():
        df = load_bars(r["Code"], end=BASELINE_END)
        if len(df) < MIN_BARS:
            continue
        bar_cache[r["Code"]] = _compute_indicators(df)
    logger.info("prepared %d bar series", len(bar_cache))
    if not bar_cache:
        raise RuntimeError("no bar series prepared")

    # 各銘柄を date->row の dict にしておく（毎日の lookup を O(1) に）
    row_by_date: dict[str, dict] = {}
    for code, df in bar_cache.items():
        row_by_date[code] = {r["date"]: r for _, r in df.iterrows()}

    all_dates = sorted(set(d for df in bar_cache.values() for d in df["date"]))
    logger.info("date range: %s to %s (%d days)", all_dates[0], all_dates[-1], len(all_dates))

    # open_positions[code] = dict(entry_date, entry_price, peak_close)
    open_positions: dict[str, dict] = {}
    closed: list[dict] = []

    for today in all_dates:
        # === exit check（既存ポジション） ===
        to_close = []
        for code, pos in open_positions.items():
            row = row_by_date[code].get(today)
            if row is None:
                continue
            cur = float(row["close"])
            ma25 = row["ma25"]
            # entry以降の最高値を更新（trailing stop基準）
            if cur > pos["peak_close"]:
                pos["peak_close"] = cur
            # 保有日数: entry_date <= d <= today の bar 数 - 1
            df = bar_cache[code]
            hold = int(((df["date"] >= pos["entry_date"]) & (df["date"] <= today)).sum()) - 1
            pnl = (cur - pos["entry_price"]) / pos["entry_price"]
            reason = None
            # 優先順位: trailing_stop → trend_break → max_hold
            drawdown_from_peak = (cur - pos["peak_close"]) / pos["peak_close"]
            if drawdown_from_peak <= TRAIL_STOP:
                reason = "trailing_stop"
            elif not pd.isna(ma25) and cur < ma25:
                reason = "trend_break"
            elif hold >= MAX_HOLD_DAYS:
                reason = "max_hold"
            if reason:
                closed.append({
                    "code": code,
                    "entry": pos["entry_date"],
                    "exit": today,
                    "pnl_pct": float(pnl),
                    "reason": reason,
                    "hold_days": hold,
                    "consensus": 2,  # 順張りは2条件AND（breakout+ma_up）。列充足用の固定値
                })
                to_close.append(code)
        for code in to_close:
            open_positions.pop(code)

        # === entry check（枠が空いていれば） ===
        if len(open_positions) < MAX_CONCURRENT:
            for code, df in bar_cache.items():
                if code in open_positions:
                    continue
                row = row_by_date[code].get(today)
                if row is None:
                    continue
                if pd.isna(row["ma25"]) or pd.isna(row["hh20_prev"]):
                    continue
                breakout = float(row["close"]) > float(row["hh20_prev"])
                ma_up = bool(row["ma25_up"])
                if breakout and ma_up:
                    px = float(row["close"])
                    open_positions[code] = {
                        "entry_date": today,
                        "entry_price": px,
                        "peak_close": px,
                    }
                    if len(open_positions) >= MAX_CONCURRENT:
                        break

    # 期末に残った建玉は強制クローズ（最終日close・reason=eod）
    last_day = all_dates[-1]
    for code, pos in open_positions.items():
        row = row_by_date[code].get(last_day)
        if row is None:
            continue
        cur = float(row["close"])
        df = bar_cache[code]
        hold = int(((df["date"] >= pos["entry_date"]) & (df["date"] <= last_day)).sum()) - 1
        closed.append({
            "code": code, "entry": pos["entry_date"], "exit": last_day,
            "pnl_pct": (cur - pos["entry_price"]) / pos["entry_price"],
            "reason": "eod", "hold_days": hold, "consensus": 2,
        })

    tdf = pd.DataFrame(closed)
    return tdf


def _attach_regime(tdf: pd.DataFrame, regimes: pd.DataFrame) -> pd.DataFrame:
    """entry_date時点のregimeを付与（Track1 label_trades_by_regime と同じ作法）。"""
    # regimes["date"] は datetime.date。trade["entry"] は load_bars 由来の
    # pandas Timestamp（Timestamp は date のサブクラスなので isinstance(date) が
    # True になり素通りすると Timestamp のまま → date キーと不一致で全 unknown 化）。
    # 必ず pure な datetime.date に正規化してから join する。
    reg_map = dict(zip(regimes["date"], regimes["regime"]))
    def _entry_date_obj(v) -> date:
        return pd.Timestamp(v).date()
    tdf = tdf.copy()
    tdf["regime"] = tdf["entry"].apply(lambda e: reg_map.get(_entry_date_obj(e), "unknown"))
    return tdf


def _wilson_ci(k: int, n: int, z: float = 1.96) -> tuple[float, float]:
    """win_rate(二項)の Wilson 95%CI。"""
    if n == 0:
        return (0.0, 0.0)
    p = k / n
    denom = 1 + z * z / n
    center = (p + z * z / (2 * n)) / denom
    half = (z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n))) / denom
    return (center - half, center + half)


def _mean_ci(xs: np.ndarray, z: float = 1.96) -> tuple[float, float]:
    """avg_return の正規近似 95%CI（mean ± z * SE）。"""
    n = len(xs)
    if n < 2:
        return (float(xs.mean()) if n else 0.0, float(xs.mean()) if n else 0.0)
    se = xs.std(ddof=1) / math.sqrt(n)
    m = xs.mean()
    return (m - z * se, m + z * se)


def report(tdf: pd.DataFrame) -> dict:
    """regime × {N, win_rate±CI, avg_return±CI, sharpe} を出力。machine-readable も返す。"""
    print("=" * 96)
    print("2-B Track2: 順張り(モメンタム/ブレイクアウト) × regime別成績")
    print(f"  universe=load_universe()  end={BASELINE_END}固定  max_concurrent={MAX_CONCURRENT}  コスト=OFF")
    print(f"  entry: close>過去{BREAKOUT_LOOKBACK}日高値 AND MA{MA_PERIOD}上向き")
    print(f"  exit : trailing_stop({TRAIL_STOP*100:.0f}%) / trend_break(close<MA{MA_PERIOD}) / max_hold({MAX_HOLD_DAYS}d)")
    print("=" * 96)

    overall = tdf["pnl_pct"].values
    print(f"全体: N={len(tdf)}  win_rate={ (overall>0).mean()*100:.1f}%  "
          f"avg_return={overall.mean()*100:+.2f}%  median={np.median(overall)*100:+.2f}%")
    # exit reason 内訳
    print("exit内訳:", tdf["reason"].value_counts().to_dict())
    print("-" * 96)

    hdr = f"{'regime':>10} {'N':>6} {'win_rate (95%CI)':>26} {'avg_return (95%CI)':>30} {'sharpe':>9}"
    print(hdr)
    print("-" * 96)

    machine: dict[str, dict] = {}
    order = ["bullish", "ranging", "bearish", "unknown"]
    regimes_present = [r for r in order if r in set(tdf["regime"])]
    for reg in regimes_present:
        grp = tdf[tdf["regime"] == reg]
        pnls = grp["pnl_pct"].values
        n = len(pnls)
        wins = int((pnls > 0).sum())
        wr = wins / n if n else 0.0
        wr_lo, wr_hi = _wilson_ci(wins, n)
        avg = float(pnls.mean()) if n else 0.0
        a_lo, a_hi = _mean_ci(pnls)
        std = float(pnls.std(ddof=1)) if n > 1 else 0.0
        sharpe = avg / std if std > 0 else 0.0
        small = "  ← 小N(注意)" if n < 30 else ""
        print(f"{reg:>10} {n:>6} "
              f"{wr*100:>6.1f}% [{wr_lo*100:5.1f},{wr_hi*100:5.1f}] "
              f"{avg*100:>+8.2f}% [{a_lo*100:+6.2f},{a_hi*100:+6.2f}] "
              f"{sharpe:>9.4f}{small}")
        machine[reg] = {
            "n": n,
            "win_rate": round(wr, 4),
            "avg_return": round(avg, 5),
        }
    print("=" * 96)
    return machine


def main():
    logging.disable(logging.WARNING)
    print("[1/3] 順張りシミュレーション中（universe全銘柄・end固定）...")
    tdf = simulate_momentum()
    print(f"      → {len(tdf)} trades 生成")

    print("[2/3] regimeラベル付与（classify_daily_regimes・entry_date時点）...")
    regimes = classify_daily_regimes()
    tdf = _attach_regime(tdf, regimes)

    print("[3/3] regime別集計 + 95%CI...")
    print()
    machine = report(tdf)

    # 参考: 本番 regime_summary() でも同じ列が回ることを確認（Track1と同engine）
    print("\n[参考] src.backtest.regime.regime_summary() 出力（Track1と同一関数）:")
    rs = regime_summary(tdf)
    cols = ["regime", "trades", "wins", "win_rate", "avg_return", "median", "std", "sharpe_per_trade"]
    print(rs[cols].to_string(index=False, float_format=lambda x: f"{x:.4f}"))

    # 最終行 machine-readable
    print("\nMOMENTUM_BY_REGIME:", machine)


if __name__ == "__main__":
    main()
