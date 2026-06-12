"""dip_depth_diagnostic.py — H1: does a DEEPER-dip executable subset survive?

問い（Grove / H1）: BNF-as-is は honest 再評価でエッジ消滅
(booked +¥2.749M → next-open both-legs re-price で gross +¥410k に崩壊。entry を
next-open へ寄せると平均 +139bps の逆行 = 反発は約定可能時点で既に起きている。さらに
~22.6bps/side のスリッページで全セルが負)。
H1 仮説: より「深い」押し目では反発が next-open までに完了しておらず、
正の executable subset が残るかもしれない。

=== 設計（ADDITIVE / READ-ONLY） ===
- 新規・独立スクリプト。engine.py/main.py/monitor.py 等の本番ファイルは触らない。
- duckdb は read_only。価格は捏造しない（next-open 欠損は honest_revalue と同じ proxy）。
- スリッページ機構は honest_revalue.py を import して再利用（同一 universe mean ~22.6bps/side）。
  honest_revalue は portfolio 集計レベルで net を出す。本スクリプトは同じ
  aggregate-impact モデル（same-name/day で order_yen 合算 → sqrt-impact / capacity）を
  保ったまま、結果の per-leg slip_bps と手数料を各トレードに帰属させ、
  per-trade net を出して bin / t-stat を計算する。

=== dip-depth ソース ===
decision_shadow.edge = SIGNAL 日（シグナルを発火させた終値の営業日）の MA25 乖離率
(負の小数, 例 -0.0599 = MA25 比 -5.99%)。entry_date は booking 日で、シグナルは
それ以前の営業日に発火している。edge は price-vs-MA25 の再計算と完全一致を確認済
(例 19250: edge=-0.06402387758533722 = 2026-05-21 終値 4453 の dev)。
decision_shadow は book 分割で (ticker, decided_date) に複数行を持つが edge は全行同一
(190 group, 0 disagreement) なので dedup(ANY_VALUE)で fan-out を回避する。

手順:
  1. positions(grove_stock.duckdb) から 326 件 signal_reversal をロード。
     decision_shadow を (ticker, decided_date=entry_date) で dedup-join し edge=dip-depth を付与。
  2. honest_revalue の next_open / adv20_and_vol / slippage プリミティブで
     entry/exit を T+1 open 再評価し、aggregate-impact で per-leg slip_bps を確定。
  3. per-trade honest net = (x_open - e_open)*shares - entry_slip_yen - exit_slip_yen
        - commission(¥0+SOR / side)。
  4. dip-depth band（固定バンド -2/-5/-10/-20%+ と分位）ごとに:
        n, mean entry next-open adverse drift(bps), honest gross/trade,
        honest net/trade, naive t-stat(mean/se)。
  5. cross-cut: 流動性(large-cap ADV>=¥10B vs small) と hold-days(0-1d vs 2d+)。
  6. best subset(n>=20 かつ honest net/trade>0): rule, n, net/trade, t-stat。
"""
from __future__ import annotations

import math
from dataclasses import dataclass

import duckdb

# --- honest_revalue のスリッページ機構を再利用 ---
import honest_revalue as hr
from honest_revalue import (
    POS_DB,
    CACHE_DB,
    OPEN_PREMIUM_BPS,
    SOR_SLIP_BPS,
    PROXY_ADVERSE,
    CAPACITY_FRAC,
    IMPACT_COEF,
    half_spread_bps,
    adv20_and_vol,
    next_open,
)

LARGE_CAP_ADV = 1e10   # ADV>=¥10B を large-cap とする(task 指定)
ENTRY_DATE_FLOOR = "2026-05-25"

# 固定 dip-depth バンド（MA25 乖離率, 負側の深さ）。
# bucket は [lo, hi) で MA25 比% を表す。深いほど lo が小さい(より負)。
# 例: shallow [-5%, -2%), deep <=-20%。
FIXED_BANDS = [
    ("(a) 0..-2%", -0.02, 0.00),
    ("(b) -2..-5%", -0.05, -0.02),
    ("(c) -5..-10%", -0.10, -0.05),
    ("(d) -10..-20%", -0.20, -0.10),
    ("(e) <=-20%", -1.00, -0.20),
]


@dataclass
class TradeRow:
    tid: int
    ticker: str
    entry_date: object
    close_date: object
    entry_price: float
    exit_price: float
    shares: int
    pnl_booked: float
    dip: float                     # decision_shadow.edge = MA25 乖離率(負)
    # 再評価結果
    e_open: float | None = None
    x_open: float | None = None
    e_date: object = None
    x_date: object = None
    e_slip_bps: float = 0.0
    x_slip_bps: float = 0.0
    e_proxy: bool = False
    x_proxy: bool = False
    e_cap_viol: bool = False
    x_cap_viol: bool = False
    adv20: float | None = None     # entry 日 ADV(流動性 cross-cut 用)
    hold_days: int | None = None
    # 派生
    entry_drift_bps: float = 0.0   # booked entry_price -> e_open の逆行(bps, +=不利)
    gross_t1: float = 0.0          # (x_open - e_open)*shares
    net: float = 0.0               # gross_t1 - slip - commission


def fetch_trades_with_dip(pos_con: duckdb.DuckDBPyConnection) -> list[TradeRow]:
    """326 件 signal_reversal を dip-depth 付きでロード(dedup-join で fan-out 回避)。"""
    rows = pos_con.execute(
        """
        WITH ds AS (
            SELECT ticker, decided_date, ANY_VALUE(edge) AS edge
            FROM decision_shadow
            GROUP BY ticker, decided_date
        )
        SELECT p.id, p.ticker, p.entry_date,
               CAST(p.closed_at AS DATE) AS close_date,
               p.entry_price, p.exit_price, p.shares, p.pnl,
               ds.edge AS dip
        FROM positions p
        LEFT JOIN ds ON ds.ticker = p.ticker AND ds.decided_date = p.entry_date
        WHERE p.exit_reason = 'signal_reversal'
          AND p.entry_date >= DATE '%s'
        ORDER BY p.entry_date, p.ticker, p.id
        """
        % ENTRY_DATE_FLOOR
    ).fetchall()
    cols = [d[0] for d in pos_con.description]
    out = []
    for r in rows:
        d = dict(zip(cols, r))
        out.append(
            TradeRow(
                tid=d["id"],
                ticker=d["ticker"],
                entry_date=d["entry_date"],
                close_date=d["close_date"],
                entry_price=float(d["entry_price"]),
                exit_price=float(d["exit_price"]) if d["exit_price"] is not None else float(d["entry_price"]),
                shares=int(d["shares"]),
                pnl_booked=float(d["pnl"]) if d["pnl"] is not None else 0.0,
                dip=float(d["dip"]) if d["dip"] is not None else float("nan"),
            )
        )
    return out


def realized_open(leg_open: float | None, booked: float) -> float:
    """honest_revalue と同一: open 欠損時は booked を基準にし逆行は slip 側で表現。"""
    return leg_open if leg_open is not None else booked


def enrich(con: duckdb.DuckDBPyConnection, trades: list[TradeRow]) -> None:
    """各トレードに T+1 open / slip_bps / drift / net を付与(in place)。

    honest_revalue と同じ aggregate-impact: same-name/同一約定日で order_yen 合算 →
    sqrt-impact と capacity を評価。per-leg の half_spread + open_premium + (proxy 加算)
    は約定単位。結果の per-leg slip_bps を各トレードに帰属させ per-trade net を出す。
    """
    # --- Pass 1: 各 leg の T+1 open を解決し、同一(ticker, 約定日)の合算を集計 ---
    entry_grp: dict[tuple[str, object], dict] = {}
    exit_grp: dict[tuple[str, object], dict] = {}
    for t in trades:
        e_open, e_date = next_open(con, t.ticker, t.entry_date)
        x_open, x_date = next_open(con, t.ticker, t.close_date)
        t.e_open, t.e_date = e_open, e_date
        t.x_open, t.x_date = x_open, x_date
        e_px = realized_open(e_open, t.entry_price)
        x_px = realized_open(x_open, t.exit_price)
        ek = (t.ticker, e_date if e_date is not None else t.entry_date)
        xk = (t.ticker, x_date if x_date is not None else t.close_date)
        entry_grp.setdefault(ek, {"shares": 0, "yen": 0.0})
        entry_grp[ek]["shares"] += t.shares
        entry_grp[ek]["yen"] += t.shares * e_px
        exit_grp.setdefault(xk, {"shares": 0, "yen": 0.0})
        exit_grp[xk]["shares"] += t.shares
        exit_grp[xk]["yen"] += t.shares * x_px

    def day_volume(code: str, d) -> float | None:
        r = con.execute(
            "SELECT volume FROM daily_quotes WHERE code=? AND date=? LIMIT 1",
            [code, d],
        ).fetchone()
        return float(r[0]) if r and r[0] is not None else None

    def leg_slip(code: str, leg_open: float | None, leg_date, booked: float,
                 grp: dict) -> tuple[float, bool, bool, float | None]:
        """honest_revalue.price_leg と同一ロジック。
        return (slip_bps, is_proxy, cap_viol, adv20)。"""
        is_proxy = leg_open is None
        ref_price = realized_open(leg_open, booked)
        asof = leg_date if leg_date is not None else None
        adv20, dvol = (None, None)
        if asof is not None:
            adv20, dvol = adv20_and_vol(con, code, asof)
        hsp = half_spread_bps(ref_price)
        g = grp.get((code, asof if asof is not None else leg_date), {"shares": 0, "yen": 0.0})
        order_yen = g["yen"] if g["yen"] > 0 else ref_price
        if adv20 and adv20 > 0 and dvol is not None:
            impact = IMPACT_COEF * math.sqrt(order_yen / adv20) * dvol
        else:
            impact = 0.0
        slip = hsp + impact + OPEN_PREMIUM_BPS
        if is_proxy:
            slip += PROXY_ADVERSE * 1e4
        cap_viol = False
        if asof is not None:
            dv = day_volume(code, asof)
            if dv and dv > 0 and g["shares"] / dv > CAPACITY_FRAC:
                cap_viol = True
        return slip, is_proxy, cap_viol, adv20

    # --- Pass 2: per-leg slip と per-trade 派生量 ---
    for t in trades:
        e_slip, e_proxy, e_cap, e_adv = leg_slip(t.ticker, t.e_open, t.e_date, t.entry_price, entry_grp)
        x_slip, x_proxy, x_cap, _ = leg_slip(t.ticker, t.x_open, t.x_date, t.exit_price, exit_grp)
        t.e_slip_bps, t.x_slip_bps = e_slip, x_slip
        t.e_proxy, t.x_proxy = e_proxy, x_proxy
        t.e_cap_viol, t.x_cap_viol = e_cap, x_cap
        t.adv20 = e_adv

        e_px = realized_open(t.e_open, t.entry_price)
        x_px = realized_open(t.x_open, t.exit_price)

        # entry next-open adverse drift: long なので booked より open が高い = 不利(+)。
        # bps = (e_open - booked)/booked * 1e4。proxy は booked 基準なので 0(別途 e_proxy flag)。
        if t.e_open is not None and t.entry_price > 0:
            t.entry_drift_bps = (t.e_open - t.entry_price) / t.entry_price * 1e4
        else:
            t.entry_drift_bps = 0.0

        t.gross_t1 = (x_px - e_px) * t.shares
        e_not = e_px * t.shares
        x_not = x_px * t.shares
        slip_yen = e_not * t.e_slip_bps / 1e4 + x_not * t.x_slip_bps / 1e4
        comm_yen = (e_not + x_not) * SOR_SLIP_BPS / 1e4   # ¥0 + 1.5bps SOR/side ブランチ
        t.net = t.gross_t1 - slip_yen - comm_yen

        # hold days(約定 open 日ベース、無ければ booked 日付)
        ed = t.e_date if t.e_date is not None else t.entry_date
        xd = t.x_date if t.x_date is not None else t.close_date
        try:
            t.hold_days = (xd - ed).days
        except Exception:
            t.hold_days = None


# ---- 統計ヘルパ ----
def mean(xs: list[float]) -> float:
    return sum(xs) / len(xs) if xs else float("nan")


def tstat(xs: list[float]) -> float:
    """naive t = mean / (std/sqrt(n))。n<2 は nan。"""
    n = len(xs)
    if n < 2:
        return float("nan")
    m = sum(xs) / n
    var = sum((x - m) ** 2 for x in xs) / (n - 1)
    if var <= 0:
        return float("inf") if m > 0 else (float("-inf") if m < 0 else 0.0)
    se = math.sqrt(var) / math.sqrt(n)
    return m / se


def fmt_bin_row(label: str, ts: list[TradeRow]) -> dict:
    n = len(ts)
    drift = [t.entry_drift_bps for t in ts if t.e_open is not None]
    gross = [t.gross_t1 for t in ts]
    net = [t.net for t in ts]
    # --- robustness: 何銘柄に支えられているか / 勝者1件を抜くと崩れるか ---
    distinct_names = len({t.ticker for t in ts})
    nets_sorted = sorted(net)
    drop_best = (sum(nets_sorted) - nets_sorted[-1]) / (n - 1) if n > 1 else float("nan")
    # 寄与トップ銘柄の net シェア(集中度)
    by_name: dict[str, float] = {}
    for t in ts:
        by_name[t.ticker] = by_name.get(t.ticker, 0.0) + t.net
    top_name_net = max(by_name.values()) if by_name else 0.0
    net_total = sum(net)
    top_share = (top_name_net / net_total) if net_total > 0 else float("nan")
    return {
        "label": label,
        "n": n,
        "mean_drift_bps": mean(drift) if drift else float("nan"),
        "gross_per_trade": mean(gross) if gross else float("nan"),
        "net_per_trade": mean(net) if net else float("nan"),
        "t_net": tstat(net),
        "net_total": net_total,
        "distinct_names": distinct_names,
        "drop_best_per_trade": drop_best,
        "top_name_share": top_share,
    }


def print_bin_table(title: str, rows: list[dict]) -> None:
    print("=" * 96)
    print(title)
    print("=" * 96)
    hdr = (f"{'bin':<16} | {'n':>4} | {'mean entry drift bps':>20} | "
           f"{'gross/trade ¥':>14} | {'NET/trade ¥':>13} | {'t(net)':>7}")
    print(hdr)
    print("-" * len(hdr))
    for r in rows:
        d = r["mean_drift_bps"]
        dstr = f"{d:+.1f}" if d == d else "  n/a"
        print(f"{r['label']:<16} | {r['n']:>4} | {dstr:>20} | "
              f"{r['gross_per_trade']:>14,.0f} | {r['net_per_trade']:>13,.0f} | "
              f"{r['t_net']:>7.2f}")
    print("-" * len(hdr))


def quintile_bands(trades: list[TradeRow]) -> list[tuple[str, list[TradeRow]]]:
    """dip(=edge) を昇順(深い→浅い、edge は負なので小さいほど深い)で5分位。"""
    valid = [t for t in trades if t.dip == t.dip]  # not nan
    valid.sort(key=lambda t: t.dip)  # 最も深い(最も負)が先頭
    n = len(valid)
    out = []
    for q in range(5):
        lo = q * n // 5
        hi = (q + 1) * n // 5 if q < 4 else n
        chunk = valid[lo:hi]
        if not chunk:
            continue
        d_lo = chunk[0].dip
        d_hi = chunk[-1].dip
        out.append((f"Q{q+1} [{d_lo*100:.1f}%..{d_hi*100:.1f}%]", chunk))
    return out


def main() -> None:
    con = duckdb.connect(CACHE_DB, read_only=True)   # daily_quotes 側
    pos_con = duckdb.connect(POS_DB, read_only=True)
    trades = fetch_trades_with_dip(pos_con)
    assert len(trades) == 326, f"expected 326 trades, got {len(trades)}"
    n_dip_missing = sum(1 for t in trades if not (t.dip == t.dip))
    enrich(con, trades)

    # universe-mean per-side slippage(honest_revalue 整合性チェック用)
    all_slip = [t.e_slip_bps for t in trades] + [t.x_slip_bps for t in trades]
    notion = []
    wbps = 0.0
    wnot = 0.0
    for t in trades:
        e_not = realized_open(t.e_open, t.entry_price) * t.shares
        x_not = realized_open(t.x_open, t.exit_price) * t.shares
        wbps += t.e_slip_bps * e_not + t.x_slip_bps * x_not
        wnot += e_not + x_not
    mean_side_bps = wbps / wnot if wnot else float("nan")

    print("#" * 96)
    print("DIP-DEPTH DIAGNOSTIC — H1: does a deeper-dip executable subset survive?")
    print("#" * 96)
    print(f"trades: {len(trades)} | distinct names: {len({t.ticker for t in trades})} | "
          f"entry_date>='{ENTRY_DATE_FLOOR}'")
    print(f"dip-depth source: decision_shadow.edge (signal-date MA25 deviation, dedup-join). "
          f"missing dip: {n_dip_missing}")
    print(f"notional-weighted MEAN per-side slippage = {mean_side_bps:.1f} bps "
          f"(honest_revalue parity check; expect ~22-23 bps)")
    booked = sum(t.pnl_booked for t in trades)
    gross_t1 = sum(t.gross_t1 for t in trades)
    net_all = sum(t.net for t in trades)
    print(f"booked gross (DB, cost=0): ¥{booked:,.0f}  |  T+1-open gross: ¥{gross_t1:,.0f}  "
          f"|  honest NET(all): ¥{net_all:,.0f}")
    # 全体 entry drift
    all_drift = [t.entry_drift_bps for t in trades if t.e_open is not None]
    print(f"ALL entry next-open adverse drift: mean={mean(all_drift):+.1f} bps "
          f"(+ = open ABOVE booked = bounce already happened; matches context +139bps claim)")
    print()

    # === 1) 固定バンド ===
    fixed_rows = []
    for label, lo, hi in FIXED_BANDS:
        ts = [t for t in trades if t.dip == t.dip and lo <= t.dip < hi]
        fixed_rows.append(fmt_bin_row(label, ts))
    print_bin_table("(1) FIXED DIP-DEPTH BANDS (MA25 deviation)", fixed_rows)
    print()

    # === 2) 分位バンド ===
    quint = quintile_bands(trades)
    quint_rows = [fmt_bin_row(lbl, ts) for lbl, ts in quint]
    print_bin_table("(2) DIP-DEPTH QUINTILES (Q1=deepest dip)", quint_rows)
    print()

    # === 3) CROSS-CUT: 流動性 ===
    large = [t for t in trades if t.adv20 is not None and t.adv20 >= LARGE_CAP_ADV]
    small = [t for t in trades if not (t.adv20 is not None and t.adv20 >= LARGE_CAP_ADV)]
    liq_rows = [
        fmt_bin_row("large ADV>=¥10B", large),
        fmt_bin_row("small/other", small),
    ]
    print_bin_table("(3) CROSS-CUT: LIQUIDITY (large-cap vs small-cap)", liq_rows)
    large_names = sorted({t.ticker for t in large})
    print(f"  large-cap names ({len(large_names)}): {', '.join(large_names) if large_names else '(none)'}")
    print()

    # === 4) CROSS-CUT: hold-days ===
    short = [t for t in trades if t.hold_days is not None and t.hold_days <= 1]
    longer = [t for t in trades if t.hold_days is not None and t.hold_days >= 2]
    hd_rows = [
        fmt_bin_row("hold 0-1d", short),
        fmt_bin_row("hold 2d+", longer),
    ]
    print_bin_table("(4) CROSS-CUT: HOLD-DAYS", hd_rows)
    print()

    # === 5) BEST SUBSET (n>=20, honest net/trade>0) ===
    # 候補: 全 bin/cut + 深いほうへの累積(<=threshold)スイープ。
    candidates: list[dict] = []
    candidates += [{**r, "src": "fixed-band"} for r in fixed_rows]
    candidates += [{**r, "src": "quintile"} for r in quint_rows]
    candidates += [{**r, "src": "liquidity"} for r in liq_rows]
    candidates += [{**r, "src": "hold-days"} for r in hd_rows]
    # 累積「deeper than X」スイープ（H1 の核心: 深い側だけ取ると正に転じるか）
    for thr in (-0.05, -0.07, -0.10, -0.12, -0.15, -0.20):
        ts = [t for t in trades if t.dip == t.dip and t.dip <= thr]
        r = fmt_bin_row(f"dip<= {thr*100:.0f}%", ts)
        candidates.append({**r, "src": "deeper-than-sweep"})

    print("=" * 96)
    print("(5) DEEPER-THAN SWEEP (cumulative: keep only trades with dip <= threshold)")
    print("=" * 96)
    hdr = f"{'rule':<16} | {'n':>4} | {'NET/trade ¥':>13} | {'t(net)':>7} | {'net total ¥':>14}"
    print(hdr)
    print("-" * len(hdr))
    for c in candidates:
        if c["src"] != "deeper-than-sweep":
            continue
        print(f"{c['label']:<16} | {c['n']:>4} | {c['net_per_trade']:>13,.0f} | "
              f"{c['t_net']:>7.2f} | {c['net_total']:>14,.0f}")
    print("-" * len(hdr))
    print()

    positive = [c for c in candidates
                if c["n"] >= 20 and c["net_per_trade"] == c["net_per_trade"]
                and c["net_per_trade"] > 0]
    positive.sort(key=lambda c: (c["t_net"] if c["t_net"] == c["t_net"] else -1e9), reverse=True)

    print("=" * 96)
    print("BEST SUBSET (honest net/trade > 0 AND n >= 20)")
    print("=" * 96)
    if not positive:
        print("  NONE. No bin/cut/sweep with n>=20 has positive honest net-per-trade.")
        best = None
    else:
        print("  (robustness: #names = distinct tickers; top%=share of +net from one name; "
              "drop-best = net/trade after removing single best trade)")
        for c in positive[:5]:
            ts = c.get("top_name_share", float("nan"))
            tsstr = f"{ts*100:.0f}%" if ts == ts else "n/a"
            print(f"  [{c['src']}] rule={c['label']:<16} n={c['n']:>4} "
                  f"net/trade=¥{c['net_per_trade']:,.0f} t={c['t_net']:.2f} "
                  f"net_total=¥{c['net_total']:,.0f} | #names={c['distinct_names']} "
                  f"top%={tsstr} drop-best=¥{c['drop_best_per_trade']:,.0f}")
        best = positive[0]

    # === VERDICT ===
    print()
    print("#" * 96)
    if best is None:
        verdict = "dead"
        reason = "no n>=20 subset has positive honest net-per-trade"
    else:
        t = best["t_net"]
        drop_best = best["drop_best_per_trade"]
        # robust = t>2 AND survives removing the single best trade AND not one-name-driven
        fragile = (drop_best == drop_best and drop_best <= 0) or \
                  (best["top_name_share"] == best["top_name_share"] and best["top_name_share"] > 0.8)
        if t == t and t > 2.0 and not fragile:
            verdict = "alive"
            reason = f"robust positive subset: {best['label']} t={t:.2f}, survives drop-best"
        elif fragile:
            verdict = "marginal"
            reason = (f"positive only on noise: best {best['label']} n={best['n']} t={t:.2f}, "
                      f"but drop-best=¥{drop_best:,.0f} / top-name supplies "
                      f"{best['top_name_share']*100:.0f}% of +net = not robust")
        else:
            verdict = "marginal"
            reason = (f"positive but weak/small-n: best {best['label']} "
                      f"n={best['n']} t={t:.2f} (<2)")
    print(f"H1 VERDICT: {verdict.upper()}  — {reason}")
    print("#" * 96)

    con.close()
    pos_con.close()
    return verdict, best, fixed_rows, quint_rows, liq_rows, hd_rows, candidates


if __name__ == "__main__":
    main()
