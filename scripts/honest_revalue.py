"""honest_revalue.py — grove-stock の edge を「現実的スリッページ」で接地評価する。

問い（Grove）: 326 件の signal_reversal トレード(+¥2.749M, modeled cost=0)の
edge は本物か? 先行検証は "inconclusive (0.1 vs 0.2%/side に依存)" で止まった。
決め手の未知数 = 「この銘柄群の現実的スリッページは幾らか?」(大型株 RT 5-12bps か
小型株 RT 40-120bps か)。それを cache の流動性データから銘柄別に推定して確定する。

=== 設計（ADDITIVE / READ-ONLY） ===
このスクリプトは新規・独立。engine.py/main.py/monitor.py 等の本番ファイルは一切
触らない。duckdb は readonly で開く。価格は捏造しない（next-open 欠損は proxy ラベル）。

手順:
  1. positions(grove_stock.duckdb)から 326 件をロード。
     OHLCV は history_cache.duckdb の daily_quotes を ticker==code(5桁)で join。
  2. entry/exit を「翌営業日 OPEN」(T+1)で再評価。entry は signal-close 時点で
     look-ahead だったので exit と対称に T+1-open へ寄せる。next-open 欠損は
     proxy(0.3% 逆行)ラベル。
  3. 流動性スケール slippage を銘柄別に計算:
       half_spread_bps = max(3, 0.5*tick/price*1e4)   (JPX tick ladder)
       impact_bps      = 0.9*sqrt(order_yen/ADV20_yen)*daily_vol_bps  (sqrt-law)
       + open_premium  = 2 bps
     ADV20_yen   = mean(close*volume) 直近20営業日(エントリー日基準)
     daily_vol   = stdev(日次リターン,20d)*1e4 (bps)
     ※ impact と capacity は「同一銘柄・同一日の合算 order_yen / 合算株数」で評価。
       理由: 本番は1銘柄を複数 book に分割発注(例 42200 を19注文)。market impact と
       板消化は同時需要の総量で決まる。分割の各注文で sqrt-impact を測ると過小評価に
       なる(19小口は安く見えるが現実は1つの大口)。spread と手数料は約定単位なので per-row。
  4. 手数料2系統(両サイド):
       (i)  ¥0 + 1.5bps SOR-slippage/side
       (ii) ワンショット ¥99(≤10万)/148(≤20万)/198(≤50万)/385(>50万) per side
  5. レポート:
       - この universe に割り当てた MEAN 現実 per-side slippage(接地数値)
       - ADV20 分布(大型/小型判定)
       - condition-1 net edge を [手数料2系統] × [slippage {computed, ×0.5, ×2}]
       - 1注文が当日出来高の >10% になる capacity 違反フラグ
"""
from __future__ import annotations

import math
from dataclasses import dataclass

import duckdb

POS_DB = "/Users/ryu/grove-stock/data/grove_stock.duckdb"
CACHE_DB = "/Users/ryu/grove-stock/data/history_cache.duckdb"

# --- モデル定数 ---
IMPACT_COEF = 0.9          # sqrt-law 係数
OPEN_PREMIUM_BPS = 2.0     # 寄り付きプレミアム(片側)
SOR_SLIP_BPS = 1.5         # ¥0手数料ブランチの SOR スリッページ(片側)
PROXY_ADVERSE = 0.003      # next-open 欠損時の代理逆行 0.3%
ADV_WINDOW = 20
VOL_WINDOW = 20
CAPACITY_FRAC = 0.10       # 当日出来高の 10% 超で capacity 違反
MIN_HALF_SPREAD_BPS = 3.0


def jpx_tick(price: float) -> float:
    """TOPIX100 以外の通常 tick ladder（task 指定の簡易版）。"""
    if price <= 3000:
        return 1.0
    if price <= 5000:
        return 5.0
    if price <= 10000:
        return 10.0
    return 50.0


def half_spread_bps(price: float) -> float:
    if price <= 0:
        return MIN_HALF_SPREAD_BPS
    return max(MIN_HALF_SPREAD_BPS, 0.5 * jpx_tick(price) / price * 1e4)


def oneshot_commission_yen(notional: float) -> float:
    """ワンショット手数料(片側), 約定代金別。"""
    if notional <= 100_000:
        return 99.0
    if notional <= 200_000:
        return 148.0
    if notional <= 500_000:
        return 198.0
    return 385.0


@dataclass
class Leg:
    """片側(entry or exit)の再評価結果。"""
    price_booked: float       # DB に記録された約定価格
    price_t1_open: float | None  # 翌営業日 open（再評価の基準）
    is_proxy: bool            # next-open 欠損→proxy 適用
    slip_bps: float           # この片側に割り当てた slippage(bps, 流動性スケール)
    capacity_violation: bool  # 当日出来高 >10%


def fetch_trades(con: duckdb.DuckDBPyConnection) -> list[dict]:
    rows = con.execute(
        """
        SELECT id, ticker, entry_price, shares, entry_date,
               exit_price, exit_reason, pnl, closed_at,
               CAST(closed_at AS DATE) AS close_date
        FROM positions
        WHERE exit_reason='signal_reversal' AND entry_date >= DATE '2026-05-25'
        ORDER BY entry_date, ticker, id
        """
    ).fetchall()
    cols = [d[0] for d in con.description]
    return [dict(zip(cols, r)) for r in rows]


def next_open(con: duckdb.DuckDBPyConnection, code: str, on_or_after) -> tuple[float | None, object]:
    """on_or_after 当日を含む最初の営業日の open を返す(その日付も)。

    entry_date / closed_at の当日に板がある(323/280件)。当日 open を T+1 約定の
    プロキシとする(signal は前場引け判定→翌寄りが現実の最速約定)。当日に無ければ
    直後の最初の営業日へ前進。全く無ければ (None, None)。
    """
    r = con.execute(
        """
        SELECT date, open FROM daily_quotes
        WHERE code = ? AND date >= ? AND open IS NOT NULL
        ORDER BY date ASC LIMIT 1
        """,
        [code, on_or_after],
    ).fetchone()
    if r is None:
        return None, None
    return float(r[1]), r[0]


def adv20_and_vol(con: duckdb.DuckDBPyConnection, code: str, asof) -> tuple[float | None, float | None]:
    """asof 日(含む)から過去 ADV_WINDOW 営業日の ADV20_yen と daily_vol_bps。"""
    rows = con.execute(
        """
        SELECT close, volume FROM daily_quotes
        WHERE code = ? AND date <= ? AND close IS NOT NULL AND volume IS NOT NULL
        ORDER BY date DESC LIMIT ?
        """,
        [code, asof, ADV_WINDOW],
    ).fetchall()
    if len(rows) < 5:  # データ不足
        return None, None
    closes = [float(c) for c, _ in rows]          # 新→旧
    vols = [float(v) for _, v in rows]
    adv20 = sum(c * v for c, v in zip(closes, vols)) / len(rows)
    # 日次リターン(隣接、新旧順は符号に無関係なので絶対値 stdev)
    rets = []
    for i in range(len(closes) - 1):
        prev = closes[i + 1]
        if prev > 0:
            rets.append((closes[i] - prev) / prev)
    if len(rets) < 2:
        return adv20, None
    mean = sum(rets) / len(rets)
    var = sum((x - mean) ** 2 for x in rets) / (len(rets) - 1)
    daily_vol_bps = math.sqrt(var) * 1e4
    return adv20, daily_vol_bps


def main() -> None:
    con = duckdb.connect(POS_DB, read_only=True)
    con.execute(f"ATTACH '{CACHE_DB}' AS h (READ_ONLY)")
    con.execute("USE h")  # daily_quotes は cache 側
    # positions は元DB。明示的に main DB を参照するため別 alias 取得。
    pos_con = duckdb.connect(POS_DB, read_only=True)
    trades = fetch_trades(pos_con)
    assert len(trades) == 326, f"expected 326 trades, got {len(trades)}"

    # --- 同一銘柄・同一日(entry)の合算 order_yen / 合算株数を先に集計（impact/capacity 用） ---
    # exit 側も同様に close_date でグルーピング。
    entry_grp: dict[tuple[str, object], dict] = {}
    exit_grp: dict[tuple[str, object], dict] = {}
    # まず entry/exit の T+1 open を引いて order_yen を確定する必要があるので2パス。

    # Pass 1: 各 leg の T+1 open を解決
    leg_cache: dict[int, dict] = {}
    for t in trades:
        e_open, e_date = next_open(con, t["ticker"], t["entry_date"])
        x_open, x_date = next_open(con, t["ticker"], t["close_date"])
        leg_cache[t["id"]] = {
            "e_open": e_open, "e_date": e_date,
            "x_open": x_open, "x_date": x_date,
        }
        # 合算キー = (ticker, 実際に約定する営業日)。open 欠損時は booked 価格で order_yen 概算。
        e_px = e_open if e_open is not None else t["entry_price"]
        x_px = x_open if x_open is not None else t["exit_price"]
        ek = (t["ticker"], e_date if e_date is not None else t["entry_date"])
        xk = (t["ticker"], x_date if x_date is not None else t["close_date"])
        entry_grp.setdefault(ek, {"shares": 0, "yen": 0.0})
        entry_grp[ek]["shares"] += t["shares"]
        entry_grp[ek]["yen"] += t["shares"] * e_px
        exit_grp.setdefault(xk, {"shares": 0, "yen": 0.0})
        exit_grp[xk]["shares"] += t["shares"]
        exit_grp[xk]["yen"] += t["shares"] * x_px

    # 当日出来高(株数) lookup ヘルパ
    def day_volume(code: str, d) -> float | None:
        r = con.execute(
            "SELECT volume FROM daily_quotes WHERE code=? AND date=? LIMIT 1",
            [code, d],
        ).fetchone()
        return float(r[0]) if r and r[0] is not None else None

    # --- 各 leg の slippage を計算 ---
    def price_leg(code: str, leg_open: float | None, leg_date, booked: float,
                  grp: dict) -> Leg:
        is_proxy = leg_open is None
        ref_price = leg_open if leg_open is not None else booked
        asof = leg_date if leg_date is not None else None
        adv20, dvol = (None, None)
        if asof is not None:
            adv20, dvol = adv20_and_vol(con, code, asof)
        hsp = half_spread_bps(ref_price)
        # 合算 order_yen / ADV で impact
        g = grp.get((code, asof if asof is not None else leg_date), {"shares": 0, "yen": 0.0})
        order_yen = g["yen"] if g["yen"] > 0 else ref_price  # フォールバック
        if adv20 and adv20 > 0 and dvol is not None:
            impact = IMPACT_COEF * math.sqrt(order_yen / adv20) * dvol
        else:
            impact = 0.0  # 流動性データ不足→spread+premium のみ(保守的に impact 0 ではなく後で proxy 加算)
        slip = hsp + impact + OPEN_PREMIUM_BPS
        if is_proxy:
            slip += PROXY_ADVERSE * 1e4  # 0.3% = 30bps を上乗せ(欠損リスク)
        # capacity: 合算株数 vs 当日出来高
        cap_viol = False
        if asof is not None:
            dv = day_volume(code, asof)
            if dv and dv > 0 and g["shares"] / dv > CAPACITY_FRAC:
                cap_viol = True
        return Leg(price_booked=booked, price_t1_open=leg_open, is_proxy=is_proxy,
                   slip_bps=slip, capacity_violation=cap_viol)

    enriched = []
    for t in trades:
        lc = leg_cache[t["id"]]
        e_leg = price_leg(t["ticker"], lc["e_open"], lc["e_date"], t["entry_price"], entry_grp)
        x_leg = price_leg(t["ticker"], lc["x_open"], lc["x_date"], t["exit_price"], exit_grp)
        enriched.append({"t": t, "entry": e_leg, "exit": x_leg})

    # === グロス(再評価): T+1 open ベース ===
    # entry を翌寄りで買い、exit を翌寄りで売る(long 前提)。
    def realized_open(leg: Leg) -> float:
        return leg.price_t1_open if leg.price_t1_open is not None else (
            leg.price_booked  # proxy: booked 価格を基準にし、逆行は slip 側で表現
        )

    gross_booked = sum(e["t"]["pnl"] for e in enriched)  # DB 記録の gross(=next-open gross 相当)
    gross_t1 = 0.0
    total_notional_rt = 0.0  # 片側 notional の合計(entry+exit)を round-trip notional とする
    for e in enriched:
        t, el, xl = e["t"], e["entry"], e["exit"]
        e_px = realized_open(el)
        x_px = realized_open(xl)
        gross_t1 += (x_px - e_px) * t["shares"]
        total_notional_rt += (e_px + x_px) * t["shares"]

    # === slippage コスト(円) ===
    # 片側 notional × slip_bps を控除。entry/exit それぞれ。
    def slip_cost_yen(scale: float) -> tuple[float, float]:
        """(total_slip_cost_yen, mean_per_side_bps_weighted)。scale は computed×倍率。"""
        total_cost = 0.0
        wsum_bps = 0.0
        wnotional = 0.0
        for e in enriched:
            t, el, xl = e["t"], e["entry"], e["exit"]
            e_px = realized_open(el)
            x_px = realized_open(xl)
            e_not = e_px * t["shares"]
            x_not = x_px * t["shares"]
            total_cost += e_not * (el.slip_bps * scale) / 1e4
            total_cost += x_not * (xl.slip_bps * scale) / 1e4
            wsum_bps += el.slip_bps * scale * e_not + xl.slip_bps * scale * x_not
            wnotional += e_not + x_not
        mean_bps = wsum_bps / wnotional if wnotional else 0.0
        return total_cost, mean_bps

    # === 手数料(円) ===
    def commission_cost_yen(branch: str) -> float:
        total = 0.0
        for e in enriched:
            t, el, xl = e["t"], e["entry"], e["exit"]
            e_not = realized_open(el) * t["shares"]
            x_not = realized_open(xl) * t["shares"]
            if branch == "zero_sor":
                total += (e_not + x_not) * SOR_SLIP_BPS / 1e4
            elif branch == "oneshot":
                total += oneshot_commission_yen(e_not) + oneshot_commission_yen(x_not)
        return total

    # --- 集計レポート ---
    n = len(enriched)
    n_proxy_entry = sum(1 for e in enriched if e["entry"].is_proxy)
    n_proxy_exit = sum(1 for e in enriched if e["exit"].is_proxy)
    n_cap_entry = sum(1 for e in enriched if e["entry"].capacity_violation)
    n_cap_exit = sum(1 for e in enriched if e["exit"].capacity_violation)

    # 接地数値: computed の mean per-side slippage bps
    slip_computed_cost, mean_per_side_bps = slip_cost_yen(1.0)

    # ADV20 分布(銘柄別, エントリー日基準のスナップショットを銘柄ごとに1点取る)
    names = sorted({e["t"]["ticker"] for e in enriched})
    adv_points = []
    for code in names:
        # その銘柄の最初のエントリー日でADVを取る
        first_date = min(e["t"]["entry_date"] for e in enriched if e["t"]["ticker"] == code)
        adv20, _ = adv20_and_vol(con, code, first_date)
        if adv20:
            adv_points.append(adv20)
    adv_points.sort()

    def pct(p: float) -> float:
        if not adv_points:
            return float("nan")
        idx = min(len(adv_points) - 1, int(p * (len(adv_points) - 1) + 0.5))
        return adv_points[idx]

    n_small = sum(1 for a in adv_points if a < 5e8)
    n_large = sum(1 for a in adv_points if a >= 1e10)

    print("=" * 78)
    print("HONEST REVALUE — grove-stock signal_reversal edge @ realistic slippage")
    print("=" * 78)
    print(f"trades: {n} | distinct names: {len(names)} | "
          f"entry_date>='2026-05-25'")
    print(f"proxy legs (next-open missing): entry={n_proxy_entry}, exit={n_proxy_exit} "
          f"(+{PROXY_ADVERSE*1e4:.0f}bps adverse each)")
    print()
    print("--- GROSS (booking vs T+1-open re-price) ---")
    print(f"  booked gross PnL (DB, modeled cost=0): ¥{gross_booked:,.0f}")
    print(f"  T+1-open re-priced gross PnL:          ¥{gross_t1:,.0f}")
    print(f"  round-trip notional (entry+exit legs): ¥{total_notional_rt:,.0f}")
    print()
    print("--- ADV20 DISTRIBUTION (per name, entry-date snapshot) ---")
    print(f"  names with ADV: {len(adv_points)}")
    print(f"  min   ¥{pct(0.0)/1e6:,.0f}M")
    print(f"  p25   ¥{pct(0.25)/1e6:,.0f}M")
    print(f"  median¥{pct(0.50)/1e6:,.0f}M")
    print(f"  p75   ¥{pct(0.75)/1e6:,.0f}M")
    print(f"  max   ¥{pct(1.0)/1e9:,.2f}B")
    print(f"  small-cap (ADV<¥500M): {n_small}/{len(adv_points)} "
          f"({100*n_small/max(1,len(adv_points)):.0f}%)  |  "
          f"large-cap (ADV>=¥10B): {n_large}/{len(adv_points)} "
          f"({100*n_large/max(1,len(adv_points)):.0f}%)")
    print()
    print("--- GROUNDED NUMBER: realistic slippage assigned to THIS universe ---")
    print(f"  notional-weighted MEAN per-side slippage = {mean_per_side_bps:.1f} bps")
    print(f"    (= half_spread + sqrt_impact[aggregate same-name/day] + {OPEN_PREMIUM_BPS:.0f}bps open)")
    # per-leg slip 分布も簡潔に
    all_slips = [e["entry"].slip_bps for e in enriched] + [e["exit"].slip_bps for e in enriched]
    all_slips.sort()
    m = len(all_slips)
    print(f"  per-leg slip bps: min={all_slips[0]:.1f} "
          f"median={all_slips[m//2]:.1f} max={all_slips[-1]:.1f}")
    print()
    print("--- CAPACITY (order > 10% of day volume, aggregate same-name/day) ---")
    print(f"  entry legs flagged: {n_cap_entry}/{n}  |  exit legs flagged: {n_cap_exit}/{n}")
    print()

    # === CONDITION-1 NET EDGE TABLE ===
    print("=" * 78)
    print("CONDITION-1 NET EDGE  (gross_T1 - slippage - commission)")
    print("=" * 78)
    branches = [
        ("(i) ¥0 commission + 1.5bps SOR/side", "zero_sor"),
        ("(ii) oneshot ¥99/148/198/385 per side", "oneshot"),
    ]
    scales = [("computed (1.0x)", 1.0), ("x0.5", 0.5), ("x2.0", 2.0)]
    header = f"{'commission branch':<40} | {'slip scenario':<16} | {'net PnL':>14}"
    print(header)
    print("-" * len(header))
    table_rows = []
    for blabel, bkey in branches:
        comm = commission_cost_yen(bkey)
        for slabel, scale in scales:
            slip_cost, _ = slip_cost_yen(scale)
            net = gross_t1 - slip_cost - comm
            print(f"{blabel:<40} | {slabel:<16} | ¥{net:>13,.0f}")
            table_rows.append((blabel, slabel, net, slip_cost, comm))
        print("-" * len(header))

    # 補助: 各ブランチの commission 総額
    print()
    for blabel, bkey in branches:
        print(f"  total commission [{blabel}]: ¥{commission_cost_yen(bkey):,.0f}")
    print(f"  total slippage [computed]:   ¥{slip_computed_cost:,.0f}")
    print(f"  total slippage [x2]:         ¥{slip_cost_yen(2.0)[0]:,.0f}")

    # === VERDICT ===
    print()
    print("=" * 78)
    # computed slippage の両 commission branch の net で判定
    net_computed = []
    for _, bkey in branches:
        comm = commission_cost_yen(bkey)
        slip_cost, _ = slip_cost_yen(1.0)
        net_computed.append(gross_t1 - slip_cost - comm)
    if all(v > 0 for v in net_computed):
        verdict = "POSITIVE"
    elif all(v < 0 for v in net_computed):
        verdict = "NEGATIVE"
    else:
        verdict = "BREAKEVEN (straddles 0 across commission branches)"
    print(f"VERDICT (computed realistic slippage, both commission branches): {verdict}")
    print(f"  net @ computed: ¥0-SOR={net_computed[0]:,.0f}  oneshot={net_computed[1]:,.0f}")
    print("=" * 78)

    con.close()
    pos_con.close()


if __name__ == "__main__":
    main()
