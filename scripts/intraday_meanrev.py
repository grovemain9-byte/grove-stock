"""intraday_meanrev.py — 引け際 BNF 式逆張り(near-close mean-reversion)戦略コア.

=== 何のためのファイルか ===
Grove の kabu 口座が稼働したら回す「引け際 約定」戦略の **純粋ロジック**。
API には一切依存しない (kabu_adapter.py が API 層、本ファイルは判断層)。
全関数が価格系列・数値だけを受け取り、numpy/dataclass だけで完結する =
口座なしの今夜でも historical データで unit-test できる。

本ファイルが実装する 5 つの判断 (タスク a-e):
  (a) dip 検出      : 日足 MA25 からの乖離率がセクター閾値 (X%) 以下なら dip-buy 候補
                      → 既存 P1 (src/players/p01.py) と同一定義を pure 関数で再現
  (b) near-close 判定: 15:30 引けの ~5分前 window に入ったら引成(後場)を発注すべきか
  (c) exit ロジック  : MA25 回帰 (乖離率 >= take_profit_dev) で手仕舞い
  (d) Tiered Kelly  : ¥2M 資本のサイジング。single-name <=0.5% pool cap + <=10% ADV cap
                      (~/.claude/rules/trading-rules.md 準拠。Pure Kelly 禁止)
  (e) kill-criterion : 実現 per-side コスト > 35bps で戦略停止 (close-exec breakeven)

=== なぜ「引け際約定」か (経済的根拠) ===
close_exec_test.py / honest_revalue.py の decomposition より:
  - booked gross (signal-day close)  = +¥2,748,800
  - これを翌 OPEN で再評価すると      = +¥410,650  ← overnight gap だけで ¥2.34M 消失
  本番シグナルは「前場引け判定 → 引け ~5分前に intraday 約定」なので overnight gap を
  払わない。引かれるのは spread + commission + 「引け5分前 ≠ 引けジャスト」の timing
  penalty のみ。@10bps/side で net ≈ +¥1.66M、breakeven は 35.1bps/side。

=== 設計原則 (ADDITIVE / READ-ONLY) ===
- 新規・独立ファイル。engine.py/main.py/monitor.py 等の本番ファイルは一切触らない。
- 価格は捏造しない。閾値・コスト定数は close_exec_test.py / strategy_params.py と整合。
- 純粋関数中心。副作用 (DB書込/発注) は持たない。テスト容易性を最優先。

参照:
  - 戦略定義   : grove-stock/CLAUDE.md (MA25乖離・コンセンサス・exit)
  - 既存パラメータ: config/strategy_params.py (DEFAULTS)
  - サイジング規範: ~/.claude/rules/trading-rules.md (Tiered Robust Kelly)
  - 経済モデル  : scripts/close_exec_test.py (gross/spread/commission/timing/breakeven)
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import time as dt_time
from typing import Sequence

# ===========================================================================
# 定数 (close_exec_test.py / strategy_params.py / trading-rules.md と整合)
# ===========================================================================

# --- 指標 (CLAUDE.md / strategy_params.DEFAULTS) ---
MA_PERIOD: int = 25                 # MA25 は日足ベース
TAKE_PROFIT_DEV: float = 0.02       # 乖離率 >= +2% で利確 (Honda asymmetric_tp / DEFAULTS)
DEFAULT_SECTOR_TH: float = -0.07    # セクター閾値 未登録時のフォールバック (DEFAULTS)
STOP_LOSS: float = -0.07            # 損切り (DEFAULTS.stop_loss)
MAX_HOLD_DAYS: int = 15             # 最大保有 (DEFAULTS.max_hold_days)

# --- near-close window (JST、引け 15:30) ---
CLOSE_TIME: dt_time = dt_time(15, 30)       # 後場引け
NEAR_CLOSE_WINDOW_MIN: int = 5              # 引け ~5分前から引成(後場)発注を許可

# --- コストモデル (close_exec_test.py と同一) ---
SOR_SLIP_BPS: float = 1.5                   # ¥0手数料ブランチの SOR スリッページ(片側)
MIN_HALF_SPREAD_BPS: float = 3.0            # JPX tick ladder 下限(片側)

# --- kill-criterion (pre-registered) ---
# close_exec_test.py: breakeven timing penalty = 35.1bps/side。
# 実現 per-side コスト (spread+commission+timing) がこれを超えたら edge は消える
# → 戦略停止。本番運用前に Grove と合意済みの不可侵ライン。
KILL_PER_SIDE_BPS: float = 35.0

# --- Tiered Robust Kelly (~/.claude/rules/trading-rules.md) ---
# Pure Kelly 禁止。Margin of Safety + Fractional Kelly + 上限。
KELLY_EDGE_MIN: float = 0.10                # edge < 10% は見送り (Margin of Safety)
# Tier: (fraction_of_kelly, max_fraction_of_bankroll)
KELLY_TIERS: dict[int, tuple[float, float]] = {
    1: (1.0 / 8.0, 0.0025),   # Tier1: 1/8 Kelly, max 0.25%
    2: (1.0 / 4.0, 0.0050),   # Tier2: 1/4 Kelly, max 0.50%
    3: (1.0 / 2.0, 0.0100),   # Tier3: 1/2 Kelly, max 1.00%
}
# single-name の「pool 上限」(オッズ破壊防止)。株式では single-name notional 上限として解釈。
SINGLE_NAME_POOL_CAP: float = 0.0050        # 1銘柄 <= bankroll 0.5%
ADV_PARTICIPATION_CAP: float = 0.10         # 1銘柄 <= 10% of ADV(出来高)


# ===========================================================================
# (補助) 指標計算 — pure
# ===========================================================================

def sma(series: Sequence[float], period: int) -> float | None:
    """単純移動平均の最新値。データ不足なら None。"""
    vals = [float(x) for x in series if x is not None and not _isnan(x)]
    if len(vals) < period:
        return None
    return sum(vals[-period:]) / period


def ma25_deviation(close_series: Sequence[float]) -> float | None:
    """MA25 乖離率 = (終値 - 日足MA25) / 日足MA25。

    CLAUDE.md / src/players/p01.py と同一定義。終値系列(日足)を渡す。
    最新終値に対する乖離を返す。データ不足/MA25=0 は None。
    """
    ma = sma(close_series, MA_PERIOD)
    if ma is None or ma == 0:
        return None
    last = float(close_series[-1])
    return (last - ma) / ma


def _isnan(x: float) -> bool:
    return isinstance(x, float) and math.isnan(x)


# ===========================================================================
# (a) dip 検出
# ===========================================================================

def detect_dip(
    close_series: Sequence[float],
    sector_threshold: float = DEFAULT_SECTOR_TH,
) -> bool:
    """MA25 乖離率 <= セクター閾値 なら dip-buy 候補 (True)。

    既存 P1 (src/players/p01.py の vote) と同一ロジックの純粋版:
        deviation = (close - MA25) / MA25
        return deviation <= threshold

    Args:
        close_series: 日足終値系列 (最新が末尾)。>=25本必要。
        sector_threshold: 負の乖離閾値 (例 薬品 -0.05、電機 -0.10)。

    Returns:
        dip 成立なら True。データ不足は False (P1 と同じ fail-safe)。
    """
    dev = ma25_deviation(close_series)
    if dev is None:
        return False
    return dev <= sector_threshold


# ===========================================================================
# (b) near-close 判定
# ===========================================================================

def should_fire_near_close(
    now: dt_time,
    *,
    close_time: dt_time = CLOSE_TIME,
    window_min: int = NEAR_CLOSE_WINDOW_MIN,
) -> bool:
    """今が引け(15:30)の ~window_min分前 window 内なら引成(後場)を発注すべき (True)。

    window = [close_time - window_min, close_time)。
    引けジャストや引け後は False (引成は引け前に出す)。

    例: close=15:30, window=5min → [15:25, 15:30) で True。
    """
    close_sec = close_time.hour * 3600 + close_time.minute * 60 + close_time.second
    now_sec = now.hour * 3600 + now.minute * 60 + now.second
    window_start = close_sec - window_min * 60
    return window_start <= now_sec < close_sec


# ===========================================================================
# (c) exit ロジック
# ===========================================================================

def should_exit(
    close_series: Sequence[float],
    entry_price: float,
    days_held: int,
    *,
    take_profit_dev: float = TAKE_PROFIT_DEV,
    stop_loss: float = STOP_LOSS,
    max_hold_days: int = MAX_HOLD_DAYS,
) -> tuple[bool, str]:
    """手仕舞い判定。(exit?, reason) を返す。

    優先順位 (DEFAULTS / monitor.py の exit 層に準拠):
      1. take_profit : 乖離率 >= take_profit_dev (MA25 回帰 / +2%)
      2. stop_loss   : (現値 - entry) / entry <= stop_loss (-7%)
      3. max_hold    : days_held >= max_hold_days
      それ以外は (False, "hold")。

    本ファイルの主眼は「MA25 回帰 exit」(signal_reversal に対応)。
    """
    if entry_price <= 0:
        return (False, "invalid_entry")
    last = float(close_series[-1])

    # 1. take-profit (MA25 回帰)
    dev = ma25_deviation(close_series)
    if dev is not None and dev >= take_profit_dev:
        return (True, "take_profit")

    # 2. stop-loss
    ret = (last - entry_price) / entry_price
    if ret <= stop_loss:
        return (True, "stop_loss")

    # 3. max-hold
    if days_held >= max_hold_days:
        return (True, "max_hold")

    return (False, "hold")


def reverts_to_ma25(close_series: Sequence[float], *, dev_threshold: float = 0.0) -> bool:
    """MA25 回帰(乖離率 >= dev_threshold)に到達したか。

    signal_reversal exit の核 (乖離が解消 = 平均回帰)。
    dev_threshold=0.0 で「MA25 に戻った」、=0.02 で「+2% 乖離」。
    """
    dev = ma25_deviation(close_series)
    if dev is None:
        return False
    return dev >= dev_threshold


# ===========================================================================
# (d) Tiered Robust Kelly サイジング (¥2M)
# ===========================================================================

@dataclass(frozen=True)
class SizingResult:
    """サイジング結果。shares=0 は見送り。"""
    shares: int
    notional: float
    tier: int
    kelly_fraction: float        # 適用した bankroll 比率 (cap 適用後)
    edge: float
    binding_constraint: str      # 何が効いたか: "edge_skip"/"pool_cap"/"adv_cap"/"kelly"/"lot_zero"
    reason: str = ""


def kelly_fraction_full(win_prob: float, win_loss_ratio: float) -> float:
    """フル Kelly 比率 f* = (b*p - q) / b。

    b = win_loss_ratio (平均利益/平均損失), p = win_prob, q = 1-p。
    負なら 0 (ベットしない)。これは「フル」値で、Tier で必ず分数倍する。
    """
    if win_loss_ratio <= 0:
        return 0.0
    p = max(0.0, min(1.0, win_prob))
    q = 1.0 - p
    f = (win_loss_ratio * p - q) / win_loss_ratio
    return max(0.0, f)


def tier_for_edge(edge: float) -> int:
    """edge(期待値の優位性)から Tier を決める。

    trading-rules.md の段階:
      edge < 10%        → Tier 0 (見送り、Margin of Safety)
      10% <= edge < 20% → Tier 1 (1/8 Kelly, max 0.25%)
      20% <= edge < 35% → Tier 2 (1/4 Kelly, max 0.50%)
      edge >= 35%       → Tier 3 (1/2 Kelly, max 1.00%)
    """
    if edge < KELLY_EDGE_MIN:
        return 0
    if edge < 0.20:
        return 1
    if edge < 0.35:
        return 2
    return 3


def tiered_kelly_size(
    *,
    bankroll: float,
    price: float,
    win_prob: float,
    win_loss_ratio: float,
    adv_shares: float | None = None,
    lot: int = 100,
) -> SizingResult:
    """Tiered Robust Kelly サイジング (¥2M 資本想定)。

    ~/.claude/rules/trading-rules.md 準拠:
      - Pure Kelly 禁止 → Tier 別に Fractional Kelly + bankroll 上限
      - edge < 10% → 見送り (Margin of Safety)
      - single-name <= 0.5% pool cap (オッズ破壊防止 / 集中度)
      - <= 10% ADV cap (出来高参加率)
      - 100株単位に丸め

    edge の定義: フル Kelly 比率 f* を「優位性」の代理に使う
      (f* が大きいほど期待値×確度が高い)。tier_for_edge で段階化。

    Returns:
        SizingResult。shares=0 なら binding_constraint が理由を示す。
    """
    if bankroll <= 0 or price <= 0:
        return SizingResult(0, 0.0, 0, 0.0, 0.0, "invalid_input", "bankroll/price<=0")

    f_full = kelly_fraction_full(win_prob, win_loss_ratio)
    edge = f_full  # フル Kelly 比率を edge proxy として使う
    tier = tier_for_edge(edge)

    if tier == 0:
        return SizingResult(0, 0.0, 0, 0.0, edge, "edge_skip",
                            f"edge {edge:.3f} < {KELLY_EDGE_MIN} (Margin of Safety)")

    kelly_frac, tier_cap = KELLY_TIERS[tier]
    # 1) Fractional Kelly を適用
    applied_frac = f_full * kelly_frac
    # 2) Tier の bankroll 上限で頭打ち
    applied_frac = min(applied_frac, tier_cap)
    # 3) single-name pool cap (<=0.5%) で頭打ち
    binding = "kelly"
    if applied_frac >= tier_cap:
        binding = "tier_cap"
    if applied_frac > SINGLE_NAME_POOL_CAP:
        applied_frac = SINGLE_NAME_POOL_CAP
        binding = "pool_cap"

    target_notional = bankroll * applied_frac
    raw_shares = int(target_notional / price)

    # 4) <=10% ADV cap (出来高参加率)
    if adv_shares is not None and adv_shares > 0:
        adv_cap_shares = int(ADV_PARTICIPATION_CAP * adv_shares)
        if raw_shares > adv_cap_shares:
            raw_shares = adv_cap_shares
            binding = "adv_cap"

    # 5) 100株単位に丸め (下方向)
    shares = (raw_shares // lot) * lot
    if shares <= 0:
        return SizingResult(0, 0.0, tier, applied_frac, edge, "lot_zero",
                            f"target ¥{target_notional:,.0f} < 1 lot @ ¥{price:,.0f}")

    notional = shares * price
    return SizingResult(shares, notional, tier, applied_frac, edge, binding,
                        f"tier{tier} frac={applied_frac:.4f} -> {shares}sh ¥{notional:,.0f}")


# ===========================================================================
# (e) kill-criterion (pre-registered)
# ===========================================================================

def jpx_tick(price: float) -> float:
    """TOPIX100 以外の通常 tick ladder (close_exec_test.py と同一)。"""
    if price <= 3000:
        return 1.0
    if price <= 5000:
        return 5.0
    if price <= 10000:
        return 10.0
    return 50.0


def half_spread_bps(price: float) -> float:
    """片側スプレッド(bps) = max(3, 0.5*tick/price*1e4)。close_exec_test.py と同一。"""
    if price <= 0:
        return MIN_HALF_SPREAD_BPS
    return max(MIN_HALF_SPREAD_BPS, 0.5 * jpx_tick(price) / price * 1e4)


def realized_cost_bps_per_side(
    *,
    exec_price: float,
    reference_price: float,
    commission_bps: float = SOR_SLIP_BPS,
) -> float:
    """実現した per-side 取引コスト(bps)。

    = |約定価格 - 参照価格(引け値など)| / 参照価格 * 1e4   (timing+slippage drift)
      + half_spread_bps(板を跨ぐ片側スプレッド)
      + commission_bps(¥0 + SOR スリッページ)

    kill-criterion はこの per-side 実現コストを監視する。
    reference_price は「引けジャスト価格」(理想約定価格)。
    """
    if reference_price <= 0:
        return float("inf")
    drift_bps = abs(exec_price - reference_price) / reference_price * 1e4
    return drift_bps + half_spread_bps(reference_price) + commission_bps


def kill_triggered(per_side_cost_bps: float, *, threshold_bps: float = KILL_PER_SIDE_BPS) -> bool:
    """実現 per-side コストが breakeven(35bps/side)を超えたら戦略停止 (True)。

    pre-registered kill rule。これを超えると close-exec edge が消える
    (close_exec_test.py: breakeven=35.1bps/side)。
    """
    return per_side_cost_bps > threshold_bps


# ===========================================================================
# 経済モデル (close_exec_test.py の再現用 — テストが叩く)
# ===========================================================================

@dataclass(frozen=True)
class Leg:
    """片側の約定 (entry または exit)。"""
    price: float
    shares: int

    @property
    def notional(self) -> float:
        return self.price * self.shares


@dataclass(frozen=True)
class RoundTripEconomics:
    """1往復 (entry+exit) の close-exec 経済。"""
    gross: float
    half_spread_cost: float
    commission_cost: float
    rt_notional: float

    def timing_cost(self, bps_per_side: float) -> float:
        return self.rt_notional * bps_per_side / 1e4

    def net(self, bps_per_side: float) -> float:
        return self.gross - self.half_spread_cost - self.commission_cost - self.timing_cost(bps_per_side)


def close_exec_economics(
    legs: Sequence[tuple[Leg, Leg, float]],
    *,
    commission_bps_per_side: float = SOR_SLIP_BPS,
) -> RoundTripEconomics:
    """複数往復の close-exec 経済を集計 (close_exec_test.py と同一モデル)。

    Args:
        legs: [(entry_leg, exit_leg, pnl), ...]。pnl は booked (signal-day close)。
              gross は SUM(pnl) で再評価しない (overnight gap を入れないため)。
        commission_bps_per_side: ¥0 + SOR(1.5bps/side)。

    Returns:
        RoundTripEconomics。net(T) / timing_cost(T) / breakeven は呼び出し側で。
    """
    gross = 0.0
    half_spread_cost = 0.0
    rt_notional = 0.0
    for entry, ex, pnl in legs:
        gross += pnl
        half_spread_cost += entry.notional * half_spread_bps(entry.price) / 1e4
        half_spread_cost += ex.notional * half_spread_bps(ex.price) / 1e4
        rt_notional += entry.notional + ex.notional
    commission_cost = rt_notional * commission_bps_per_side / 1e4
    return RoundTripEconomics(gross, half_spread_cost, commission_cost, rt_notional)


def breakeven_timing_bps(econ: RoundTripEconomics) -> float:
    """net=0 になる timing penalty(bps/side)。close_exec_test.py と同一式。

    net(T) = gross - half_spread - commission - rt_notional*T/1e4 = 0
      → T = (gross - half_spread - commission) / rt_notional * 1e4
    """
    if econ.rt_notional <= 0:
        return 0.0
    residual = econ.gross - econ.half_spread_cost - econ.commission_cost
    return residual / econ.rt_notional * 1e4


# ===========================================================================
# CLI (手動確認用 — テストは tests/ から import する)
# ===========================================================================

def _demo() -> None:
    """口座なしで判断ロジックの sanity を表示 (発注はしない)。"""
    print("intraday_meanrev — pure logic demo (NO API, NO order)")
    print("-" * 60)
    # dip: 26本の系列で最後が MA25 から -8%
    series = [1000.0] * 25 + [920.0]
    dev = ma25_deviation(series)
    print(f"(a) dip: dev={dev:.3%}  detect_dip(th=-7%)={detect_dip(series, -0.07)}")
    # near-close
    print(f"(b) near-close @15:26={should_fire_near_close(dt_time(15, 26))} "
          f"@15:20={should_fire_near_close(dt_time(15, 20))} "
          f"@15:30={should_fire_near_close(dt_time(15, 30))}")
    # exit: 回帰系列
    ser2 = [1000.0] * 24 + [1000.0, 1030.0]
    ex, why = should_exit(ser2, entry_price=950.0, days_held=2)
    print(f"(c) exit: {ex} reason={why} (dev={ma25_deviation(ser2):.3%})")
    # sizing
    r = tiered_kelly_size(bankroll=2_000_000, price=2000.0,
                          win_prob=0.58, win_loss_ratio=1.2, adv_shares=500_000)
    print(f"(d) sizing(¥2M): shares={r.shares} notional=¥{r.notional:,.0f} "
          f"tier={r.tier} frac={r.kelly_fraction:.4f} binding={r.binding_constraint}")
    # kill
    c = realized_cost_bps_per_side(exec_price=2010.0, reference_price=2000.0)
    print(f"(e) kill: per_side_cost={c:.1f}bps kill_triggered={kill_triggered(c)} "
          f"(threshold={KILL_PER_SIDE_BPS}bps)")


if __name__ == "__main__":
    _demo()
