"""h1_independent_check.py — ADVERSARIAL independent verification of H1 diagnostic.

Reimplements EVERYTHING from raw duckdb. Does NOT import dip_depth_diagnostic
or honest_revalue. Goal: (a) reproduce best-subset honest net/trade, (b) decide
if it is real vs luckiest-of-K under multiple testing, (c) sanity-check economics.

ADDITIVE / READ-ONLY. New file under scripts/. No production file touched.
duckdb opened read_only. No price fabrication (next-open missing => proxy label,
mirroring honest_revalue's published mechanism so the comparison is apples-apples).
"""
from __future__ import annotations

import math
import duckdb

POS_DB = "/Users/ryu/grove-stock/data/grove_stock.duckdb"
CACHE_DB = "/Users/ryu/grove-stock/data/history_cache.duckdb"

# Model constants — copied as NUMERIC LITERALS (not imported) to match the
# published honest_revalue spec, so any drift in the imported module is caught.
IMPACT_COEF = 0.9
OPEN_PREMIUM_BPS = 2.0
SOR_SLIP_BPS = 1.5
PROXY_ADVERSE_BPS = 30.0      # 0.3% = 30 bps
CAPACITY_FRAC = 0.10
MIN_HALF_SPREAD_BPS = 3.0
ADV_WINDOW = 20
ENTRY_FLOOR = "2026-05-25"


def jpx_tick(p):
    if p <= 3000: return 1.0
    if p <= 5000: return 5.0
    if p <= 10000: return 10.0
    return 50.0


def half_spread_bps(p):
    if p <= 0: return MIN_HALF_SPREAD_BPS
    return max(MIN_HALF_SPREAD_BPS, 0.5 * jpx_tick(p) / p * 1e4)


def next_open(cache, code, on_or_after):
    r = cache.execute(
        "SELECT date, open FROM daily_quotes WHERE code=? AND date>=? AND open IS NOT NULL "
        "ORDER BY date ASC LIMIT 1", [code, on_or_after]).fetchone()
    return (float(r[1]), r[0]) if r else (None, None)


def adv20_and_vol(cache, code, asof):
    rows = cache.execute(
        "SELECT close, volume FROM daily_quotes WHERE code=? AND date<=? AND close IS NOT NULL "
        "AND volume IS NOT NULL ORDER BY date DESC LIMIT ?", [code, asof, ADV_WINDOW]).fetchall()
    if len(rows) < 5: return None, None
    closes = [float(c) for c, _ in rows]; vols = [float(v) for _, v in rows]
    adv = sum(c * v for c, v in zip(closes, vols)) / len(rows)
    rets = [(closes[i] - closes[i+1]) / closes[i+1] for i in range(len(closes)-1) if closes[i+1] > 0]
    if len(rets) < 2: return adv, None
    m = sum(rets) / len(rets)
    var = sum((x-m)**2 for x in rets) / (len(rets)-1)
    return adv, math.sqrt(var) * 1e4


def day_volume(cache, code, d):
    r = cache.execute("SELECT volume FROM daily_quotes WHERE code=? AND date=? LIMIT 1", [code, d]).fetchone()
    return float(r[0]) if r and r[0] is not None else None


def load_trades(pos, cache):
    rows = pos.execute(
        """
        WITH ds AS (SELECT ticker, decided_date, ANY_VALUE(edge) AS edge
                    FROM decision_shadow GROUP BY ticker, decided_date)
        SELECT p.id, p.ticker, p.entry_date, CAST(p.closed_at AS DATE) AS cd,
               p.entry_price, p.exit_price, p.shares, p.pnl, ds.edge AS dip
        FROM positions p
        LEFT JOIN ds ON ds.ticker=p.ticker AND ds.decided_date=p.entry_date
        WHERE p.exit_reason='signal_reversal' AND p.entry_date >= DATE '%s'
        ORDER BY p.entry_date, p.ticker, p.id
        """ % ENTRY_FLOOR).fetchall()
    cols = [d[0] for d in pos.description]
    trades = [dict(zip(cols, r)) for r in rows]
    # resolve legs
    for t in trades:
        t["entry_price"] = float(t["entry_price"])
        t["exit_price"] = float(t["exit_price"]) if t["exit_price"] is not None else t["entry_price"]
        t["shares"] = int(t["shares"])
        t["pnl"] = float(t["pnl"]) if t["pnl"] is not None else 0.0
        t["dip"] = float(t["dip"]) if t["dip"] is not None else float("nan")
        t["e_open"], t["e_date"] = next_open(cache, t["ticker"], t["entry_date"])
        t["x_open"], t["x_date"] = next_open(cache, t["ticker"], t["cd"])
    # aggregate same-name/day order_yen & shares (impact + capacity)
    egrp, xgrp = {}, {}
    for t in trades:
        e_px = t["e_open"] if t["e_open"] is not None else t["entry_price"]
        x_px = t["x_open"] if t["x_open"] is not None else t["exit_price"]
        ek = (t["ticker"], t["e_date"] if t["e_date"] is not None else t["entry_date"])
        xk = (t["ticker"], t["x_date"] if t["x_date"] is not None else t["cd"])
        egrp.setdefault(ek, {"shares": 0, "yen": 0.0}); egrp[ek]["shares"] += t["shares"]; egrp[ek]["yen"] += t["shares"]*e_px
        xgrp.setdefault(xk, {"shares": 0, "yen": 0.0}); xgrp[xk]["shares"] += t["shares"]; xgrp[xk]["yen"] += t["shares"]*x_px

    def leg_slip(code, leg_open, leg_date, booked, grp):
        is_proxy = leg_open is None
        ref = leg_open if leg_open is not None else booked
        adv = dvol = None
        if leg_date is not None:
            adv, dvol = adv20_and_vol(cache, code, leg_date)
        hsp = half_spread_bps(ref)
        g = grp.get((code, leg_date if leg_date is not None else None), {"shares": 0, "yen": 0.0})
        order_yen = g["yen"] if g["yen"] > 0 else ref
        impact = IMPACT_COEF * math.sqrt(order_yen / adv) * dvol if (adv and adv > 0 and dvol is not None) else 0.0
        slip = hsp + impact + OPEN_PREMIUM_BPS + (PROXY_ADVERSE_BPS if is_proxy else 0.0)
        cap = False
        if leg_date is not None:
            dv = day_volume(cache, code, leg_date)
            if dv and dv > 0 and g["shares"] / dv > CAPACITY_FRAC: cap = True
        return slip, is_proxy, cap, adv

    for t in trades:
        e_slip, e_proxy, e_cap, e_adv = leg_slip(t["ticker"], t["e_open"], t["e_date"], t["entry_price"], egrp)
        x_slip, x_proxy, x_cap, _ = leg_slip(t["ticker"], t["x_open"], t["x_date"], t["exit_price"], xgrp)
        e_px = t["e_open"] if t["e_open"] is not None else t["entry_price"]
        x_px = t["x_open"] if t["x_open"] is not None else t["exit_price"]
        t["adv20"] = e_adv
        t["entry_drift_bps"] = (t["e_open"] - t["entry_price"]) / t["entry_price"] * 1e4 if (t["e_open"] is not None and t["entry_price"] > 0) else 0.0
        t["gross_t1"] = (x_px - e_px) * t["shares"]
        e_not = e_px * t["shares"]; x_not = x_px * t["shares"]
        slip_yen = e_not * e_slip / 1e4 + x_not * x_slip / 1e4
        comm_yen = (e_not + x_not) * SOR_SLIP_BPS / 1e4
        t["net"] = t["gross_t1"] - slip_yen - comm_yen
        t["e_slip_bps"] = e_slip; t["x_slip_bps"] = x_slip
        t["e_not"] = e_not; t["x_not"] = x_not
        ed = t["e_date"] if t["e_date"] is not None else t["entry_date"]
        xd = t["x_date"] if t["x_date"] is not None else t["cd"]
        try: t["hold_days"] = (xd - ed).days
        except Exception: t["hold_days"] = None
    return trades


def tstat(xs):
    n = len(xs)
    if n < 2: return float("nan")
    m = sum(xs) / n
    var = sum((x-m)**2 for x in xs) / (n-1)
    if var <= 0: return float("inf") if m > 0 else (float("-inf") if m < 0 else 0.0)
    return m / (math.sqrt(var) / math.sqrt(n))


def jackknife_drop_best(net):
    """net/trade after removing the single most positive trade."""
    if len(net) < 2: return float("nan")
    s = sorted(net)
    return (sum(s) - s[-1]) / (len(net) - 1)


def jackknife_drop_name(trades):
    """net/trade after removing the single most positive NAME (all its trades)."""
    by = {}
    for t in trades:
        by.setdefault(t["ticker"], []).append(t["net"])
    if len(by) < 2: return float("nan")
    name_tot = {k: sum(v) for k, v in by.items()}
    worst_keep = max(name_tot, key=name_tot.get)  # the most positive name -> drop it
    kept = [t["net"] for t in trades if t["ticker"] != worst_keep]
    return sum(kept) / len(kept) if kept else float("nan")


def subset_stats(label, ts):
    net = [t["net"] for t in ts]
    names = {t["ticker"] for t in ts}
    by = {}
    for t in ts: by[t["ticker"]] = by.get(t["ticker"], 0.0) + t["net"]
    tot = sum(net)
    top_name_net = max(by.values()) if by else 0.0
    return {
        "label": label, "n": len(ts), "n_names": len(names),
        "net_per_trade": (sum(net)/len(net)) if net else float("nan"),
        "net_total": tot, "t": tstat(net),
        "drop_best_trade": jackknife_drop_best(net),
        "drop_best_name": jackknife_drop_name(ts),
        "top_name_share": (top_name_net/tot) if tot > 0 else float("nan"),
        "mean_drift_bps": (sum(t["entry_drift_bps"] for t in ts if t["e_open"] is not None) /
                           max(1, sum(1 for t in ts if t["e_open"] is not None))),
    }


def main():
    pos = duckdb.connect(POS_DB, read_only=True)
    cache = duckdb.connect(CACHE_DB, read_only=True)
    trades = load_trades(pos, cache)
    assert len(trades) == 326, len(trades)

    # parity vs published context numbers
    booked = sum(t["pnl"] for t in trades)
    gross_t1 = sum(t["gross_t1"] for t in trades)
    net_all = sum(t["net"] for t in trades)
    drift_all = [t["entry_drift_bps"] for t in trades if t["e_open"] is not None]
    wbps = sum(t["e_slip_bps"]*t["e_not"] + t["x_slip_bps"]*t["x_not"] for t in trades)
    wnot = sum(t["e_not"] + t["x_not"] for t in trades)
    print("### PARITY ###")
    print(f"n=326 booked=¥{booked:,.0f} T+1gross=¥{gross_t1:,.0f} honestNET=¥{net_all:,.0f}")
    print(f"mean entry drift={sum(drift_all)/len(drift_all):+.1f}bps  mean/side slip={wbps/wnot:.1f}bps")
    print()

    # ----- enumerate EVERY cut the diagnostic tried (the multiple-testing family) -----
    valid = [t for t in trades if t["dip"] == t["dip"]]
    cuts = []  # (family, label, subset)

    # fixed bands
    for lbl, lo, hi in [("0..-2%", -0.02, 0.0), ("-2..-5%", -0.05, -0.02),
                        ("-5..-10%", -0.10, -0.05), ("-10..-20%", -0.20, -0.10),
                        ("<=-20%", -1.0, -0.20)]:
        cuts.append(("fixed", lbl, [t for t in valid if lo <= t["dip"] < hi]))
    # quintiles (Q1 deepest)
    vs = sorted(valid, key=lambda t: t["dip"]); n = len(vs)
    for q in range(5):
        lo = q*n//5; hi = (q+1)*n//5 if q < 4 else n
        cuts.append(("quintile", f"Q{q+1}", vs[lo:hi]))
    # liquidity
    large = [t for t in trades if t["adv20"] is not None and t["adv20"] >= 1e10]
    small = [t for t in trades if not (t["adv20"] is not None and t["adv20"] >= 1e10)]
    cuts.append(("liquidity", "large>=10B", large)); cuts.append(("liquidity", "small/other", small))
    # hold-days
    cuts.append(("hold", "0-1d", [t for t in trades if t["hold_days"] is not None and t["hold_days"] <= 1]))
    cuts.append(("hold", "2d+", [t for t in trades if t["hold_days"] is not None and t["hold_days"] >= 2]))
    # deeper-than sweep
    for thr in (-0.05, -0.07, -0.10, -0.12, -0.15, -0.20):
        cuts.append(("sweep", f"dip<={thr*100:.0f}%", [t for t in valid if t["dip"] <= thr]))

    print("### ALL CUTS (the multiple-testing family) ###")
    print(f"{'family':<10} {'label':<12} {'n':>4} {'names':>5} {'net/trade':>11} {'t':>6} "
          f"{'dropBestTr':>10} {'dropBestNm':>10} {'topNm%':>7}")
    allrows = []
    for fam, lbl, ts in cuts:
        s = subset_stats(lbl, ts); s["family"] = fam; allrows.append(s)
        tns = f"{s['top_name_share']*100:.0f}%" if s["top_name_share"] == s["top_name_share"] else "n/a"
        print(f"{fam:<10} {lbl:<12} {s['n']:>4} {s['n_names']:>5} {s['net_per_trade']:>11,.0f} "
              f"{s['t']:>6.2f} {s['drop_best_trade']:>10,.0f} {s['drop_best_name']:>10,.0f} {tns:>7}")
    print()
    K = len(allrows)

    # ----- multiple-testing accounting -----
    pos_n20 = [r for r in allrows if r["n"] >= 20 and r["net_per_trade"] == r["net_per_trade"] and r["net_per_trade"] > 0]
    pos_any = [r for r in allrows if r["net_per_trade"] == r["net_per_trade"] and r["net_per_trade"] > 0]
    print("### MULTIPLE TESTING ###")
    print(f"total cuts tried K = {K}")
    print(f"cuts with positive net/trade (any n): {len(pos_any)}")
    print(f"cuts with positive net/trade AND n>=20: {len(pos_n20)}")
    if pos_n20:
        best = max(pos_n20, key=lambda r: r["t"])
        t = best["t"]; n = best["n"]
        # two-sided naive p from t (normal approx for the per-trade mean)
        p_naive = 2 * (1 - 0.5*(1+math.erf(abs(t)/math.sqrt(2))))
        alpha = 0.05
        bonf = alpha / K
        print(f"best positive (n>=20): rule='{best['label']}' n={n} names={best['n_names']} "
              f"net/trade=¥{best['net_per_trade']:,.0f} t={t:.2f}")
        print(f"  naive two-sided p (normal approx) = {p_naive:.3f}")
        print(f"  Bonferroni threshold alpha/K = 0.05/{K} = {bonf:.4f}")
        print(f"  survives Bonferroni? {'YES' if p_naive < bonf else 'NO'}")
        # expected number of false positives among K under null (one-sided positive)
        exp_fp_pos = K * 0.5 * (1 - 0.5*(1+math.erf((t)/math.sqrt(2)))) * 2  # rough
        print(f"  drop-best-trade=¥{best['drop_best_trade']:,.0f}  "
              f"drop-best-NAME=¥{best['drop_best_name']:,.0f}  top-name-share={best['top_name_share']*100:.0f}%")
        # luckiest-of-K sanity: max-t expected under null
        # P(max of K t-stats > t) approx = 1-(1-p_one)^K
        p_one = 1 - 0.5*(1+math.erf(t/math.sqrt(2)))
        p_fwer = 1 - (1 - p_one)**K
        print(f"  one-sided p={p_one:.3f}; P(at least one of K >= this t under null) = {p_fwer:.3f}")
    else:
        print("NO positive subset with n>=20. H1 dead on its face.")
    print()

    # ----- single best NAME contribution to the headline <=-15% cut -----
    print("### HEADLINE CUT FORENSICS (dip<=-15%) ###")
    cut15 = [t for t in valid if t["dip"] <= -0.15]
    by = {}
    for t in cut15: by.setdefault(t["ticker"], []).append(t)
    s = subset_stats("dip<=-15%", cut15)
    print(f"n={s['n']} names={s['n_names']} net/trade=¥{s['net_per_trade']:,.0f} "
          f"net_total=¥{s['net_total']:,.0f} t={s['t']:.2f}")
    print("  per-name net contribution:")
    for nm in sorted(by, key=lambda k: -sum(x["net"] for x in by[k])):
        sub = by[nm]; tot = sum(x["net"] for x in sub)
        print(f"    {nm}: n={len(sub)} net=¥{tot:,.0f} (share of total={tot/s['net_total']*100:.0f}%)")
    # drop the single best individual trade
    print(f"  drop single best TRADE -> net/trade=¥{s['drop_best_trade']:,.0f}")
    print(f"  drop single best NAME  -> net/trade=¥{s['drop_best_name']:,.0f}")
    pos.close(); cache.close()


if __name__ == "__main__":
    main()
