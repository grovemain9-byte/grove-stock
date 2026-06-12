"""close_exec_test.py — grove-stock の edge を「near-close 約定」で再評価する。

=== Grove の verdict 再オープン（2026-06-10） ===
先行検証(honest_revalue.py)は NEXT-OPEN 約定だけをテストして net=-¥1.19M、edge=dead
と結論した。しかし Grove の指摘で誤りが判明:

  honest_revalue の損失は「両レッグを翌日 OPEN で再評価」したことが主因。
  entry を signal-day close で買い、翌 OPEN で買い直す = +130bps の OVERNIGHT GAP を
  毎回払う構図。だが本番シグナルは前場引け判定→ au カブコム API なら「引け直前(~5分前)」に
  intraday 約定できる。これは overnight gap を回避し、booked price(≈signal-day close)で
  約定できる。つまり gross は +¥2.749M(booked)のまま残り、引かれるのは
    spread + commission + 「引け5分前 ≠ 引けジャスト」の timing penalty
  だけ。

honest_revalue の decomposition がこれを裏付ける:
  booked gross(close)        = +¥2,748,800
  T+1-open 再評価 gross       = +¥410,650   ← overnight gap だけで ¥2.338M 消失
  - slippage ¥1.50M - comm ¥0.10M → net -¥1.19M

→ near-close 約定はこの ¥2.338M gap を払わない。本スクリプトはそれを定量化する。

=== 設計（ADDITIVE / READ-ONLY） ===
新規・独立。engine.py/main.py/monitor.py 等の本番ファイルは触らない。duckdb は readonly。
価格は捏造しない。next-open を一切引かない(gap を入れないため)。booked price をそのまま
near-close 約定価格の近似として使う。

モデル（両レッグ = entry + exit、long 前提）:
  gross_close = SUM(pnl)                       # = booked gross, 再評価しない
  half_spread = per-name JPX tick ladder の片側スプレッド(bps) × leg notional
  commission  = ¥0 + 1.5bps SOR / side          (honest_revalue と同一)
  timing_pen  = T bps / side × leg notional      (T ∈ {5,10,20} で感度)
                「引け5分前に約定 ≠ 引けジャスト」のドリフトコスト。両サイドに課す。
  net(T)      = gross_close - half_spread - commission - timing_pen(T)

レポート:
  - 各 timing penalty(5/10/20bps/side)での close-exec net
  - 比較対象: 先行 next-open net = -¥1,188,401(¥0-SOR branch)
  - breakeven timing penalty(bps/side): net=0 になる T
"""
from __future__ import annotations

import duckdb

POS_DB = "/Users/ryu/grove-stock/data/grove_stock.duckdb"

# 凍結スナップショット境界 (2026-06-11 追加)。paper pipeline は毎日新規 close を積む
# ため、上限なしクエリは日々ドリフトし assert 326 が壊れる (06-10:+3件, 06-11:+4件を
# 観測)。本スクリプトの anchor (326 trades / gross +¥2,748,800 / breakeven 35.1bps)
# は 2026-06-09 close までの母集団の性質として凍結する。
SNAPSHOT_CLOSE_DATE = "2026-06-09"

# --- モデル定数（honest_revalue.py と整合） ---
SOR_SLIP_BPS = 1.5          # ¥0手数料ブランチの SOR スリッページ(片側)
MIN_HALF_SPREAD_BPS = 3.0   # JPX tick ladder 下限(片側)
TIMING_PENALTIES_BPS = [5.0, 10.0, 20.0]  # near-close timing penalty 感度(片側)
PRIOR_NEXT_OPEN_NET = -1_188_401.0        # honest_revalue: ¥0-SOR + computed slip
REALISTIC_TIMING_CEIL_BPS = 10.0          # 「現実的」上限(verdict 判定の閾値, 片側)


def jpx_tick(price: float) -> float:
    """TOPIX100 以外の通常 tick ladder（honest_revalue と同一の簡易版）。"""
    if price <= 3000:
        return 1.0
    if price <= 5000:
        return 5.0
    if price <= 10000:
        return 10.0
    return 50.0


def half_spread_bps(price: float) -> float:
    """片側スプレッド(bps) = max(3, 0.5*tick/price*1e4)。"""
    if price <= 0:
        return MIN_HALF_SPREAD_BPS
    return max(MIN_HALF_SPREAD_BPS, 0.5 * jpx_tick(price) / price * 1e4)


def fetch_trades(con: duckdb.DuckDBPyConnection) -> list[dict]:
    rows = con.execute(
        """
        SELECT id, ticker, entry_price, shares, entry_date,
               exit_price, exit_reason, pnl, closed_at
        FROM positions
        WHERE exit_reason='signal_reversal' AND entry_date >= DATE '2026-05-25'
          AND CAST(closed_at AS DATE) <= CAST(? AS DATE)
        ORDER BY entry_date, ticker, id
        """,
        [SNAPSHOT_CLOSE_DATE],
    ).fetchall()
    cols = [d[0] for d in con.description]
    return [dict(zip(cols, r)) for r in rows]


def main() -> None:
    con = duckdb.connect(POS_DB, read_only=True)
    trades = fetch_trades(con)
    assert len(trades) == 326, f"expected 326 trades, got {len(trades)}"

    # === gross: booked(=signal-day close), 再評価しない ===
    # pnl == (exit_price - entry_price) * shares なので SUM(pnl) が close-exec gross。
    gross_close = sum(t["pnl"] for t in trades)

    # === per-leg half-spread コスト(円) ===
    # entry(buy) と exit(sell) 各々の notional × half_spread_bps。
    # near-close でも板の bid/ask を跨ぐので片側スプレッドは必ず払う。
    half_spread_cost = 0.0
    entry_notional = 0.0
    exit_notional = 0.0
    leg_spread_bps: list[float] = []  # 分布用
    for t in trades:
        e_not = t["entry_price"] * t["shares"]
        x_not = t["exit_price"] * t["shares"]
        entry_notional += e_not
        exit_notional += x_not
        e_sp = half_spread_bps(t["entry_price"])
        x_sp = half_spread_bps(t["exit_price"])
        half_spread_cost += e_not * e_sp / 1e4
        half_spread_cost += x_not * x_sp / 1e4
        leg_spread_bps.append(e_sp)
        leg_spread_bps.append(x_sp)
    rt_notional = entry_notional + exit_notional  # round-trip(両レッグ)notional

    # === commission: ¥0 + 1.5bps SOR / side ===
    commission_cost = rt_notional * SOR_SLIP_BPS / 1e4

    # === timing penalty(円): T bps/side × 両レッグ notional ===
    def timing_cost(bps_per_side: float) -> float:
        return rt_notional * bps_per_side / 1e4

    def net_at(bps_per_side: float) -> float:
        return gross_close - half_spread_cost - commission_cost - timing_cost(bps_per_side)

    # === breakeven timing penalty(bps/side): net=0 ===
    # net(T) = gross_close - half_spread - commission - rt_notional*T/1e4 = 0
    #   → T = (gross_close - half_spread - commission) / rt_notional * 1e4
    residual_before_timing = gross_close - half_spread_cost - commission_cost
    breakeven_bps = residual_before_timing / rt_notional * 1e4

    # --- 集計 ---
    n = len(trades)
    names = sorted({t["ticker"] for t in trades})
    wmean_spread_bps = (half_spread_cost / rt_notional * 1e4) if rt_notional else 0.0
    leg_spread_bps.sort()
    msp = len(leg_spread_bps)

    print("=" * 78)
    print("CLOSE-EXEC TEST — grove-stock signal_reversal edge @ NEAR-CLOSE execution")
    print("=" * 78)
    print(f"trades: {n} | distinct names: {len(names)} | entry_date>='2026-05-25'")
    print("model: book BOTH legs at signal-day CLOSE (booked price); subtract")
    print("       half-spread + commission(¥0+1.5bps SOR) + near-close TIMING penalty.")
    print("       NO next-open re-pricing → avoids the +130bps overnight gap.")
    print()
    print("--- GROSS (close-exec, NOT re-priced) ---")
    print(f"  booked gross PnL (= SUM(pnl), signal-day close):  ¥{gross_close:,.0f}")
    print(f"  entry-leg notional:                               ¥{entry_notional:,.0f}")
    print(f"  exit-leg  notional:                               ¥{exit_notional:,.0f}")
    print(f"  round-trip notional (both legs):                  ¥{rt_notional:,.0f}")
    print()
    print("--- DEDUCTIONS (always paid, even near-close) ---")
    print(f"  half-spread (JPX tick ladder, both legs):         ¥{half_spread_cost:,.0f}")
    print(f"    notional-weighted mean per-side spread = {wmean_spread_bps:.2f} bps")
    print(f"    per-leg spread bps: min={leg_spread_bps[0]:.2f} "
          f"median={leg_spread_bps[msp//2]:.2f} max={leg_spread_bps[-1]:.2f}")
    print(f"  commission (¥0 + {SOR_SLIP_BPS}bps SOR/side):              ¥{commission_cost:,.0f}")
    print()
    print("=" * 78)
    print("CLOSE-EXEC NET  vs  NEAR-CLOSE TIMING PENALTY (sensitivity)")
    print("=" * 78)
    print("  net(T) = gross_close - half_spread - commission - rt_notional*T/1e4")
    print()
    header = f"  {'timing penalty / side':<26} | {'timing cost ¥':>14} | {'CLOSE-EXEC net ¥':>18}"
    print(header)
    print("  " + "-" * (len(header) - 2))
    for bps in TIMING_PENALTIES_BPS:
        tc = timing_cost(bps)
        net = net_at(bps)
        flag = ""
        if bps <= REALISTIC_TIMING_CEIL_BPS and net > 0:
            flag = "  <- realistic & POSITIVE"
        print(f"  {f'{bps:.0f} bps/side':<26} | ¥{tc:>13,.0f} | ¥{net:>17,.0f}{flag}")
    print("  " + "-" * (len(header) - 2))
    print()
    print("--- COMPARISON ---")
    print(f"  prior NEXT-OPEN net (honest_revalue, ¥0-SOR + computed slip): "
          f"¥{PRIOR_NEXT_OPEN_NET:,.0f}")
    print(f"  delta (close-exec @10bps  −  next-open): "
          f"¥{net_at(10.0) - PRIOR_NEXT_OPEN_NET:,.0f}")
    print(f"    → the overnight gap that next-open paid but near-close avoids "
          f"≈ ¥{gross_close - 410_650:,.0f} of gross")
    print()
    print("--- BREAKEVEN ---")
    print(f"  residual before timing (gross - spread - commission): "
          f"¥{residual_before_timing:,.0f}")
    print(f"  BREAKEVEN timing penalty (net=0): {breakeven_bps:.1f} bps/side")
    print(f"    (= you would have to be {breakeven_bps:.0f}bps/side worse than the close "
          f"to wipe the edge)")
    print()

    # === VERDICT ===
    # alive  = net clearly positive at realistic timing penalty(<=10bps/side)
    # dead   = net negative even at 5bps/side
    # marginal = straddles
    net5 = net_at(5.0)
    net10 = net_at(10.0)
    net20 = net_at(20.0)
    if net5 < 0:
        verdict = "DEAD"
    elif net10 > 0:
        verdict = "ALIVE"
    else:
        verdict = "MARGINAL"
    print("=" * 78)
    print(f"VERDICT (near-close execution): {verdict}")
    print(f"  net @5bps={net5:,.0f}  @10bps={net10:,.0f}  @20bps={net20:,.0f}")
    print(f"  breakeven={breakeven_bps:.1f}bps/side  "
          f"(realistic ceiling for 'alive' = {REALISTIC_TIMING_CEIL_BPS:.0f}bps/side)")
    print("=" * 78)

    con.close()


if __name__ == "__main__":
    main()
