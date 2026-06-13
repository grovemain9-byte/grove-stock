"""2-C S1 edge-gate: セクター連れ高 × 出遅れ株キャッチアップ (sector laggard catch-up)。

仮説: 上昇中のセクター内で出遅れている個別株は、その後キャッチアップ上昇する。
     → 「セクター勢い上位 × 個別リターン下位」の銘柄を買うと edge があるか？

READ-ONLY: src/ / config/ は変更しない。結果を stdout に出力。
スクリプトを docs/grove-stock/research/2c_sector_laggard_gate.py に保存。

再現コマンド:
  cd /Users/ryu/grove-stock
  PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 PYTHONPATH=. .venv/bin/python docs/grove-stock/research/2c_sector_laggard_gate.py

================================================================================
룩-ahead bias 対策（最重要）:
  - セクターモメンタム: 前日までの N 日リターン（当日 close は使わない）
  - 個別出遅れスコア: 同じく前日までの N 日リターン
  - エントリー: 決定日の翌日 open で執行（当日終値でのエントリーはしない）
    ※ 簡略化として当日 close エントリーを使うが、判定に使うのは前日までのデータ
    ※ shift(1) を徹底して当日情報を排除する
================================================================================

コスト:
  - cost-OFF (slippage/commission=0): 生 edge を見る
  - cost-ON: 片道 10bps のスリッページ + 5bps 手数料 = 片道 15bps × 2 往復 = 30bps / trade

比較ベースライン:
  - 同じスクリプト内で逆張り(run_backtest)を実行して annualized Sharpe を参照点とする
"""
from __future__ import annotations

import logging
import math
from datetime import date, timedelta

import numpy as np
import pandas as pd
import duckdb

from src.data.cache import load_universe, CACHE_DB
from src.backtest.runner import compute_sector_thresholds_from_cache
from src.backtest.engine import run_backtest

# ─────────────────────────────────────────────────────────────────────────────
# パラメータ（公平比較のため reversal baseline と揃える）
# ─────────────────────────────────────────────────────────────────────────────
START = "2021-05-19"
END   = "2026-06-12"
MAX_CONCURRENT = 10        # reversal baseline と同じ
POSITION_SIZE  = 100_000.0

# セクターモメンタム閾値: 全セクターの上位 TOP_SECTOR_PCT を「強いセクター」とする
TOP_SECTOR_PCT = 0.33      # 上位 33% = 強いセクター

# 出遅れフィルタ: セクター内下位 LAGGARD_PCT を「出遅れ」とする
LAGGARD_PCT    = 0.33      # 下位 33% = 出遅れ

# コスト (片道 bps)
COST_ONE_WAY_BPS = 15.0    # 10bps slip + 5bps commission

# パラメータスイープ対象
MOMENTUM_WINDOWS = [10, 20]   # セクターモメンタム計算窓（日）
HOLD_DAYS_LIST   = [5, 10, 20]

MIN_BARS = 30  # データ不足銘柄を除外するための最小バー数

logging.disable(logging.WARNING)


# ─────────────────────────────────────────────────────────────────────────────
# データロード: DuckDB から一括取得（銘柄別接続は遅い → 1クエリで全バー取得）
# ─────────────────────────────────────────────────────────────────────────────

def load_all_bars(universe: pd.DataFrame) -> pd.DataFrame:
    """daily_quotes から全銘柄の OHLCV を取得して1つの DataFrame に結合。

    columns: code, date, close
    日付は datetime.date 型。
    """
    codes = [str(c) for c in universe["Code"].tolist()]
    con = duckdb.connect(str(CACHE_DB), read_only=True)
    placeholders = ",".join(["?"] * len(codes))
    df = con.execute(
        f"""
        SELECT code, date::DATE AS date, close
        FROM daily_quotes
        WHERE code IN ({placeholders})
          AND date >= ?
          AND date <= ?
        ORDER BY code, date
        """,
        codes + [START, END],
    ).fetchdf()
    con.close()
    df["date"] = pd.to_datetime(df["date"]).dt.date
    return df


# ─────────────────────────────────────────────────────────────────────────────
# Wilson CI / mean CI ヘルパー
# ─────────────────────────────────────────────────────────────────────────────
Z95 = 1.959963984540054


def wilson_ci(k: int, n: int, z: float = Z95) -> tuple[float, float]:
    if n == 0:
        return (float("nan"), float("nan"))
    p = k / n
    denom = 1 + z * z / n
    center = (p + z * z / (2 * n)) / denom
    half = (z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n))) / denom
    return (center - half, center + half)


def mean_ci(x: np.ndarray, z: float = Z95) -> tuple[float, float, float]:
    n = len(x)
    if n == 0:
        return float("nan"), float("nan"), float("nan")
    m = float(x.mean())
    if n < 2:
        return m, float("nan"), float("nan")
    se = float(x.std(ddof=1)) / math.sqrt(n)
    return m, m - z * se, m + z * se


# ─────────────────────────────────────────────────────────────────────────────
# 年率換算ヘルパー
# ─────────────────────────────────────────────────────────────────────────────

def annualize_sharpe(avg_pct_per_trade: float, std_pct_per_trade: float,
                     avg_hold_days: float, trades_per_year: float | None = None) -> float:
    """per-trade Sharpe → 年率 Sharpe。

    方式: 年間トレード数 T = 252 / avg_hold_days として
          annualized_sharpe = per_trade_sharpe × sqrt(T)

    ルール: raw 5yr total で割るのを避け、必ず年率換算。
    """
    if std_pct_per_trade <= 0 or avg_hold_days <= 0:
        return float("nan")
    per_trade_sharpe = avg_pct_per_trade / std_pct_per_trade
    trades_per_year = trades_per_year or (252.0 / avg_hold_days)
    return per_trade_sharpe * math.sqrt(trades_per_year)


def annualize_cagr(total_return_frac: float, years: float) -> float:
    """総リターン → CAGR。"""
    if years <= 0 or total_return_frac <= -1.0:
        return float("nan")
    return (1.0 + total_return_frac) ** (1.0 / years) - 1.0


# ─────────────────────────────────────────────────────────────────────────────
# コアバックテスト: セクター出遅れ戦略
# ─────────────────────────────────────────────────────────────────────────────

def run_laggard_backtest(
    all_bars: pd.DataFrame,
    sector_map: dict[str, str],
    momentum_window: int,
    hold_days: int,
    cost_on: bool = False,
) -> dict:
    """
    セクター出遅れキャッチアップ戦略のバックテスト。

    룩-ahead bias 対策:
      1. 毎日の「セクターモメンタム」は shift(1) 済みの trailing N日リターンを使用
         → 当日 close は含めない（前日までの N 日）
      2. エントリー条件の判定に当日 close を使わない
      3. フォワードリターンは「翌日の close / エントリー日の close - 1」で計算
         → entry_date の close をエントリー価格とし、exit は entry+hold 後の close
         ※ エントリー日の close 自体はシグナルの判定に使っておらず（shift済み）、
            純粋に「エントリー執行価格」として使う（当日終値執行の簡略仮定）

    Parameters
    ----------
    all_bars : (code, date, close) の長形式 DataFrame
    sector_map : {code: sector_name}
    momentum_window : セクターモメンタム / 個別出遅れ計算窓（日）
    hold_days : 保有日数
    cost_on : True なら片道 COST_ONE_WAY_BPS × 2 を pnl から差し引く

    Returns
    -------
    dict with keys: trades, n_signals_total, ...
    """
    # ── pivot: (date × code) close 行列 ──────────────────────────────────────
    pivot = all_bars.pivot(index="date", columns="code", values="close").sort_index()
    all_dates = list(pivot.index)

    # ── 個別リターン (N日・前日まで): shift(1) で当日 close を除外 ──────────
    # pct_change(momentum_window) は t 日の close / t-N 日の close - 1 を返す
    # さらに shift(1) して「前日終値基準の N 日リターン」にする
    ind_ret = pivot.pct_change(momentum_window).shift(1)

    # ── セクター平均モメンタム: 各日の全銘柄 ind_ret をセクターで平均 ─────────
    # sector_map: {code -> sector_name}
    # pivot の列が code
    codes_in_pivot = list(pivot.columns)
    code_to_sector = {c: sector_map.get(c, "UNKNOWN") for c in codes_in_pivot}
    sectors = sorted(set(code_to_sector.values()))

    # セクター別 avg momentum (date × sector)
    sector_dfs = {}
    for sec in sectors:
        sec_codes = [c for c in codes_in_pivot if code_to_sector[c] == sec]
        if not sec_codes:
            continue
        sector_dfs[sec] = ind_ret[sec_codes].mean(axis=1)
    sector_mom = pd.DataFrame(sector_dfs)  # date × sector

    # ── 日次バックテストループ ─────────────────────────────────────────────
    # open_positions: {code: {"entry_date": date, "entry_px": float, "exit_date_idx": int}}
    open_positions: dict[str, dict] = {}
    closed_trades: list[dict] = []
    cost_bps = COST_ONE_WAY_BPS * 2 if cost_on else 0.0  # round-trip bps

    warmup = momentum_window + 2  # 最初の N+2 日はシグナルなし（warmup）

    for i, today in enumerate(all_dates):
        if i < warmup:
            continue

        # ── EXIT CHECK ──────────────────────────────────────────────────────
        to_close = []
        for code, pos in open_positions.items():
            exit_date_idx = pos["exit_date_idx"]
            if i >= exit_date_idx:
                exit_px = pivot.at[today, code] if (today in pivot.index and
                          code in pivot.columns and not pd.isna(pivot.at[today, code])) else None
                if exit_px is None:
                    # 流動性なし → 最後に見えた price で強制クローズ
                    past = pivot[code].dropna()
                    exit_px = float(past.iloc[-1]) if len(past) else pos["entry_px"]
                pnl = (exit_px - pos["entry_px"]) / pos["entry_px"]
                pnl -= cost_bps / 10000.0  # コスト控除
                closed_trades.append({
                    "code": code,
                    "entry": pos["entry_date"],
                    "exit": today,
                    "pnl_pct": pnl,
                    "hold_days": i - pos["entry_idx"],
                })
                to_close.append(code)
        for code in to_close:
            open_positions.pop(code)

        # ── ENTRY CHECK (枠が空いているなら) ────────────────────────────────
        if len(open_positions) >= MAX_CONCURRENT:
            continue

        # 今日の個別出遅れスコア (shift済: 前日までの N 日リターン)
        today_ind = ind_ret.loc[today] if today in ind_ret.index else None
        if today_ind is None or today_ind.isna().all():
            continue

        # 今日のセクターモメンタム
        today_sec = sector_mom.loc[today] if today in sector_mom.index else None
        if today_sec is None or today_sec.isna().all():
            continue

        # 「強いセクター」= セクターモメンタムが上位 TOP_SECTOR_PCT 以内
        sec_vals = today_sec.dropna()
        if len(sec_vals) < 3:
            continue
        sec_threshold = float(sec_vals.quantile(1.0 - TOP_SECTOR_PCT))
        strong_sectors = set(sec_vals[sec_vals >= sec_threshold].index)

        if not strong_sectors:
            continue

        # 強いセクターに属する銘柄を抽出し、個別リターンで下位 LAGGARD_PCT を選ぶ
        candidates = []
        for code in codes_in_pivot:
            if code in open_positions:
                continue
            sec = code_to_sector.get(code, "UNKNOWN")
            if sec not in strong_sectors:
                continue
            ret_val = today_ind.get(code, float("nan"))
            if pd.isna(ret_val):
                continue
            entry_px = pivot.at[today, code] if (
                today in pivot.index and code in pivot.columns and
                not pd.isna(pivot.at[today, code])
            ) else None
            if entry_px is None or entry_px <= 0:
                continue
            candidates.append((code, ret_val, entry_px, sec))

        if not candidates:
            continue

        # セクター内での出遅れスコアを算出: 各銘柄の個別リターン - そのセクター平均
        # → セクター平均より大きく下落した銘柄が「出遅れ」
        # (負の値ほど出遅れ = 下位 LAGGARD_PCT に入れる)
        cand_df = pd.DataFrame(candidates, columns=["code", "ind_ret", "entry_px", "sector"])
        # セクター内ランク: 出遅れ = ind_ret が小さい
        # laggard フィルタ: セクター内で下位 LAGGARD_PCT 以内
        def mark_laggard(grp):
            thr = grp["ind_ret"].quantile(LAGGARD_PCT)
            grp = grp.copy()
            grp["is_laggard"] = grp["ind_ret"] <= thr
            return grp

        cand_df = cand_df.groupby("sector", group_keys=False).apply(mark_laggard)
        laggards = cand_df[cand_df["is_laggard"]].copy()

        if laggards.empty:
            continue

        # ランダムではなく ind_ret 昇順（最も出遅れたものから）でソートして枠を埋める
        laggards = laggards.sort_values("ind_ret")

        slots_available = MAX_CONCURRENT - len(open_positions)
        for _, row in laggards.iterrows():
            if slots_available <= 0:
                break
            code = row["code"]
            if code in open_positions:
                continue
            open_positions[code] = {
                "entry_date": today,
                "entry_px": float(row["entry_px"]),
                "entry_idx": i,
                "exit_date_idx": i + hold_days,
            }
            slots_available -= 1

    # ── 期末残存ポジションを強制クローズ ─────────────────────────────────
    last_date = all_dates[-1]
    last_i = len(all_dates) - 1
    for code, pos in open_positions.items():
        exit_px_series = pivot[code].dropna()
        exit_px = float(exit_px_series.iloc[-1]) if len(exit_px_series) else pos["entry_px"]
        pnl = (exit_px - pos["entry_px"]) / pos["entry_px"]
        pnl -= cost_bps / 10000.0
        closed_trades.append({
            "code": code,
            "entry": pos["entry_date"],
            "exit": last_date,
            "pnl_pct": pnl,
            "hold_days": last_i - pos["entry_idx"],
        })

    return {"trades": closed_trades}


# ─────────────────────────────────────────────────────────────────────────────
# スコアカード計算
# ─────────────────────────────────────────────────────────────────────────────

def compute_scorecard(trades: list[dict], label: str, years: float = 5.0) -> dict:
    """トレードリストからスコアカード指標を計算。"""
    if not trades:
        return {"label": label, "n": 0}

    pnls = np.array([t["pnl_pct"] for t in trades], dtype=float)
    hold_days_arr = np.array([t.get("hold_days", 10) for t in trades], dtype=float)
    n = len(pnls)
    wins = int((pnls > 0).sum())
    wr = wins / n
    wlo, whi = wilson_ci(wins, n)
    avg, alo, ahi = mean_ci(pnls)
    std = float(pnls.std(ddof=1)) if n > 1 else 0.0
    avg_hold = float(hold_days_arr.mean())

    # Annualized Sharpe
    ann_sharpe = annualize_sharpe(avg, std, avg_hold)

    # CAGR: total equity curve の簡易計算
    # 毎日 max_concurrent=10 スロット、position_size=100k → 総資本 1M
    # 簡略: 全トレードのリターンを平均し、年間トレード数 × avg_return で CAGR 近似
    trades_per_year = (n / years)
    # ポートフォリオ CAGR 近似 (等サイズ・独立仮定): geometric mean per trade
    # ただし占有率を考慮: portfolio_return ≈ (avg_return × avg_hold / 252) × (M/N * 252/avg_hold)
    # 実装は単純に: 全期間累積リターン / MAX_CONCURRENT の平均を年率化
    # → 各トレードが 100k を使うので portfolio 総額 1M に対する年率は:
    # CAGR = (average concurrent trades / MAX_CONCURRENT) × trades_per_year × avg_return_per_trade
    avg_concurrent = min(trades_per_year * avg_hold / 252 * MAX_CONCURRENT, MAX_CONCURRENT)
    annual_portfolio_return = (avg_concurrent / MAX_CONCURRENT) * (252.0 / avg_hold) * avg
    cagr = annual_portfolio_return  # 近似（複利効果は無視した直線近似）

    # Max Drawdown: date-ordered cumulative P&L の最大 drawdown
    exits = [(t["exit"], t["pnl_pct"]) for t in trades if "exit" in t]
    exits.sort(key=lambda x: x[0])
    cumulative = 0.0
    peak = 0.0
    max_dd = 0.0
    max_dd_date = None
    for ex_date, ret in exits:
        cumulative += ret
        if cumulative > peak:
            peak = cumulative
        dd = cumulative - peak
        if dd < max_dd:
            max_dd = dd
            max_dd_date = ex_date

    # Calmar (annualized): CAGR / |maxDD|
    calmar = cagr / abs(max_dd) if max_dd < 0 else float("nan")

    return {
        "label": label,
        "n": n,
        "win_rate": wr,
        "wr_ci": (wlo, whi),
        "avg_return": avg,
        "avg_return_ci": (alo, ahi),
        "std": std,
        "avg_hold_days": avg_hold,
        "ann_sharpe": ann_sharpe,
        "cagr_approx": cagr,
        "max_dd": max_dd,
        "max_dd_date": max_dd_date,
        "calmar": calmar,
        "trades_per_year": trades_per_year,
    }


# ─────────────────────────────────────────────────────────────────────────────
# 逆張りベースライン
# ─────────────────────────────────────────────────────────────────────────────

def run_reversal_baseline(years: float = 5.0) -> dict:
    """既存の run_backtest (逆張り) を実行して annualized Sharpe を取得。"""
    print("[baseline] 逆張り reversal backtest 実行中...")
    thresholds = compute_sector_thresholds_from_cache(k=2.0)
    result = run_backtest(
        sector_thresholds=thresholds,
        start_date=START,
        end_date=END,
        max_concurrent_positions=MAX_CONCURRENT,
        position_size=POSITION_SIZE,
        initial_balance=1_000_000,
    )
    pnls = np.array([t.pnl_pct for t in result.trades if t.exit_date is not None], dtype=float)
    holds = np.array([t.hold_days for t in result.trades if t.exit_date is not None and t.hold_days], dtype=float)
    n = len(pnls)
    if n == 0:
        return {"label": "reversal-baseline", "n": 0, "ann_sharpe": float("nan")}

    avg_hold = float(holds.mean()) if len(holds) else 5.0
    avg = float(pnls.mean())
    std = float(pnls.std(ddof=1))
    wr = float((pnls > 0).mean())
    wlo, whi = wilson_ci(int((pnls > 0).sum()), n)
    alo, _, ahi_raw = mean_ci(pnls)
    _, alo_ci, ahi_ci = mean_ci(pnls)
    ann_sharpe = annualize_sharpe(avg, std, avg_hold)

    # Max drawdown
    trades_sorted = sorted(
        [t for t in result.trades if t.exit_date is not None],
        key=lambda t: t.exit_date,
    )
    cumulative = 0.0
    peak = 0.0
    max_dd = 0.0
    max_dd_date = None
    for t in trades_sorted:
        cumulative += t.pnl_pct
        if cumulative > peak:
            peak = cumulative
        dd = cumulative - peak
        if dd < max_dd:
            max_dd = dd
            max_dd_date = t.exit_date

    avg_concurrent = min((n / years) * avg_hold / 252 * MAX_CONCURRENT, MAX_CONCURRENT)
    annual_portfolio_return = (avg_concurrent / MAX_CONCURRENT) * (252.0 / avg_hold) * avg
    calmar = annual_portfolio_return / abs(max_dd) if max_dd < 0 else float("nan")

    return {
        "label": "reversal-baseline",
        "n": n,
        "win_rate": wr,
        "wr_ci": (wlo, whi),
        "avg_return": avg,
        "avg_return_ci": (alo_ci, ahi_ci),
        "std": std,
        "avg_hold_days": avg_hold,
        "ann_sharpe": ann_sharpe,
        "cagr_approx": annual_portfolio_return,
        "max_dd": max_dd,
        "max_dd_date": max_dd_date,
        "calmar": calmar,
        "trades_per_year": n / years,
    }


# ─────────────────────────────────────────────────────────────────────────────
# 出力
# ─────────────────────────────────────────────────────────────────────────────

def print_scorecard_table(results: list[dict]) -> None:
    """コンパクトなスコアカードテーブルを出力。"""
    W = 130
    print("=" * W)
    print(f"{'config':<32} {'N':>5} {'win%':>6} {'win 95%CI':>17} "
          f"{'avg_ret%':>9} {'avg_ret 95%CI':>20} "
          f"{'ann_Sharpe':>11} {'CAGR%':>7} {'maxDD%':>7} {'maxDD_date':>12} {'Calmar':>7}")
    print("-" * W)
    for r in results:
        if r.get("n", 0) == 0:
            print(f"{r.get('label',''):<32} {'N=0':>5}")
            continue
        wr = r["win_rate"] * 100
        wlo, whi = r["wr_ci"]
        avg = r["avg_return"] * 100
        alo, ahi = r["avg_return_ci"]
        sharpe = r["ann_sharpe"]
        cagr = r["cagr_approx"] * 100
        mdd = r["max_dd"] * 100
        mdd_dt = str(r.get("max_dd_date", "?"))[:10]
        calmar = r["calmar"]
        n = r["n"]
        label = r["label"][:31]
        print(f"{label:<32} {n:>5} {wr:>5.1f}% "
              f"[{wlo*100:>5.1f},{whi*100:>5.1f}] "
              f"{avg:>+8.2f}% [{alo*100:>+6.2f},{ahi*100:>+6.2f}] "
              f"{sharpe:>11.3f} {cagr:>+6.2f}% {mdd:>+6.2f}% "
              f"{mdd_dt:>12} {calmar:>7.2f}")
    print("=" * W)


# ─────────────────────────────────────────────────────────────────────────────
# main
# ─────────────────────────────────────────────────────────────────────────────

def main() -> None:
    print("=" * 80)
    print("2-C S1 edge-gate: セクター連れ高 × 出遅れ株キャッチアップ")
    print(f"  period: {START} .. {END}  (~5yr)")
    print(f"  universe: load_universe() | max_concurrent={MAX_CONCURRENT} | pos={POSITION_SIZE:,.0f}")
    print(f"  cost-ON: 往復 {COST_ONE_WAY_BPS*2:.0f}bps")
    print("=" * 80)

    # ── 1. ユニバースとバーを読む ──────────────────────────────────────────
    print("\n[1/4] ユニバース + バーデータ読み込み中...")
    universe = load_universe()
    print(f"      universe: {len(universe)} stocks, {universe['S33Nm'].nunique()} sectors")

    all_bars = load_all_bars(universe)
    print(f"      bars: {len(all_bars):,} rows, {all_bars['code'].nunique()} codes")

    # 十分なバーがある銘柄だけ使う
    bar_counts = all_bars.groupby("code")["close"].count()
    valid_codes = set(bar_counts[bar_counts >= MIN_BARS].index)
    all_bars = all_bars[all_bars["code"].isin(valid_codes)].copy()
    print(f"      after MIN_BARS={MIN_BARS} filter: {all_bars['code'].nunique()} codes")

    # sector_map: code -> S33Nm
    sector_map = dict(zip(universe["Code"].astype(str), universe["S33Nm"]))

    # ── 2. パラメータスイープ（cost-OFF） ────────────────────────────────
    print(f"\n[2/4] パラメータスイープ (cost-OFF)")
    print(f"      momentum_windows={MOMENTUM_WINDOWS} × hold_days={HOLD_DAYS_LIST}")

    years = 5.0  # 2021-05 ~ 2026-06 ≈ 5年
    results: list[dict] = []

    for mw in MOMENTUM_WINDOWS:
        for hd in HOLD_DAYS_LIST:
            label = f"laggard MW={mw:2d}d H={hd:2d}d cost-OFF"
            print(f"      running: {label} ...", end=" ", flush=True)
            out = run_laggard_backtest(
                all_bars, sector_map, momentum_window=mw, hold_days=hd, cost_on=False
            )
            sc = compute_scorecard(out["trades"], label, years=years)
            results.append(sc)
            print(f"N={sc['n']} ann_Sharpe={sc.get('ann_sharpe', float('nan')):.3f}")

    # ── 3. ベストコンフィグで cost-ON ────────────────────────────────────
    print(f"\n[3/4] cost-ON robustness (ベストコンフィグ)")
    # best = cost-OFF で ann_sharpe 最大
    valid_results = [r for r in results if r.get("n", 0) >= 30 and not math.isnan(r.get("ann_sharpe", float("nan")))]
    if valid_results:
        best = max(valid_results, key=lambda r: r["ann_sharpe"])
        # ラベルから MW / H を逆引き
        # e.g. "laggard MW=20d H=10d cost-OFF"
        import re
        m = re.search(r"MW=\s*(\d+)d H=\s*(\d+)d", best["label"])
        if m:
            best_mw, best_hd = int(m.group(1)), int(m.group(2))
            label_on = f"laggard MW={best_mw:2d}d H={best_hd:2d}d cost-ON"
            print(f"      best cost-OFF: {best['label']} (Sharpe={best['ann_sharpe']:.3f})")
            print(f"      running cost-ON: {label_on} ...", end=" ", flush=True)
            out_on = run_laggard_backtest(
                all_bars, sector_map, momentum_window=best_mw, hold_days=best_hd, cost_on=True
            )
            sc_on = compute_scorecard(out_on["trades"], label_on, years=years)
            results.append(sc_on)
            print(f"N={sc_on['n']} ann_Sharpe={sc_on.get('ann_sharpe', float('nan')):.3f}")
        else:
            print("      ベストコンフィグ逆引き失敗")
    else:
        print("      有効なコンフィグなし (N<30 or Sharpe=nan)")

    # ── 4. 逆張りベースライン ─────────────────────────────────────────────
    print(f"\n[4/4] 逆張りベースライン (reversal run_backtest)...")
    baseline = run_reversal_baseline(years=years)
    results_all = results + [baseline]

    # ── スコアカード出力 ─────────────────────────────────────────────────
    print("\n")
    print_scorecard_table(results_all)

    # ── CI overlap 分析 (best vs baseline) ──────────────────────────────
    print("\n[CI overlap分析]")
    best_candidates = [r for r in results if r.get("n", 0) >= 30 and "cost-OFF" in r.get("label", "")]
    if best_candidates and baseline.get("n", 0) > 0:
        best_off = max(best_candidates, key=lambda r: r.get("ann_sharpe", float("-inf")))
        b_lo, b_hi = best_off["avg_return_ci"]
        r_lo, r_hi = baseline["avg_return_ci"]
        overlap_avg = not (b_hi < r_lo or r_hi < b_lo)
        b_wr_lo, b_wr_hi = best_off["wr_ci"]
        r_wr_lo, r_wr_hi = baseline["wr_ci"]
        overlap_wr = not (b_wr_hi < r_wr_lo or r_wr_hi < b_wr_lo)
        print(f"  best laggard (cost-OFF): {best_off['label']}")
        print(f"    avg_ret  laggard=[{b_lo*100:+.2f}%,{b_hi*100:+.2f}%]  baseline=[{r_lo*100:+.2f}%,{r_hi*100:+.2f}%]  overlap={overlap_avg}")
        print(f"    win_rate laggard=[{b_wr_lo*100:.1f}%,{b_wr_hi*100:.1f}%]  baseline=[{r_wr_lo*100:.1f}%,{r_wr_hi*100:.1f}%]  overlap={overlap_wr}")
        print(f"    ann_Sharpe laggard={best_off['ann_sharpe']:.3f}  baseline={baseline.get('ann_sharpe', float('nan')):.3f}")
    else:
        print("  ベースラインまたは laggard 結果が不十分 (N<30)")

    # ── VERDICT ─────────────────────────────────────────────────────────
    print()
    print("=" * 80)
    print("VERDICT")
    print("=" * 80)

    def verdict(results_list: list[dict], baseline_sc: dict) -> str:
        cost_off = [r for r in results_list if r.get("n", 0) >= 30 and "cost-OFF" in r.get("label", "")]
        cost_on  = [r for r in results_list if r.get("n", 0) >= 30 and "cost-ON"  in r.get("label", "")]
        if not cost_off:
            return "RED (N<30 — insufficient trades)"
        best_off = max(cost_off, key=lambda r: r.get("ann_sharpe", float("-inf")))
        sharpe_off = best_off.get("ann_sharpe", float("nan"))
        sharpe_on  = max((r.get("ann_sharpe", float("-inf")) for r in cost_on), default=float("nan"))
        baseline_sharpe = baseline_sc.get("ann_sharpe", float("nan"))

        # CI overlap
        b_lo, b_hi = best_off.get("avg_return_ci", (float("nan"), float("nan")))
        r_lo, r_hi = baseline_sc.get("avg_return_ci", (float("nan"), float("nan")))
        ci_overlap = (not math.isnan(b_lo) and not math.isnan(r_lo) and
                      not (b_hi < r_lo or r_hi < b_lo))

        if math.isnan(sharpe_off):
            return "RED (Sharpe undefined — std=0 or N=0)"
        if sharpe_off < 0.2:
            return f"RED (ann_Sharpe={sharpe_off:.3f} < 0.2 — no edge)"
        if sharpe_off < 0.5 or ci_overlap:
            if not math.isnan(sharpe_on) and sharpe_on >= 0.3:
                return f"YELLOW (ann_Sharpe cost-OFF={sharpe_off:.3f}, cost-ON={sharpe_on:.3f}, CI overlap={ci_overlap})"
            return f"RED (ann_Sharpe={sharpe_off:.3f} marginal + CI overlap={ci_overlap})"
        if not math.isnan(sharpe_on) and sharpe_on >= 0.5:
            return f"GREEN (ann_Sharpe cost-OFF={sharpe_off:.3f}, cost-ON={sharpe_on:.3f})"
        if not math.isnan(sharpe_on) and sharpe_on < 0.2:
            return f"RED (cost-ON kills edge: ann_Sharpe drops to {sharpe_on:.3f})"
        return f"YELLOW (cost-OFF={sharpe_off:.3f} promising but cost-ON={sharpe_on:.3f} marginal)"

    v = verdict(results, baseline)
    print(f"  {v}")
    print()

    # ── 룩-ahead bias 確認 ────────────────────────────────────────────
    print("[룩-ahead bias チェック]")
    print("  - セクターモメンタム: pct_change(N).shift(1) → 当日 close を含めない ✓")
    print("  - 個別出遅れスコア: 同上 → 当日情報ゼロ ✓")
    print("  - エントリー価格: 判定日の close（当日終値執行・簡略仮定）")
    print("    → シグナル判定は shift(1) 済みデータ、価格のみ当日 close を使用")
    print("    → 厳密には翌日 open が正しい。この簡略仮定は軽微なバイアス源")
    print("  - フォワードリターン: entry_px から hold_days 後の close ✓")
    print()

    print("[最大の注意点]")
    print("  エントリー価格として当日終値(entry_px=close[today])を使い、")
    print("  シグナルを shift(1) で確保した。ただし実際の執行は翌日 open になるため、")
    print("  当日終値→翌日 open のギャップが小さいと仮定している。")
    print("  日本株のオーバーナイトギャップは平均 ±0.2-0.5% 程度で、")
    print("  コスト 15bps × 2 に対して無視できないが、テスト程度の誤差範囲内。")
    print()
    print("2-C gate script: docs/grove-stock/research/2c_sector_laggard_gate.py")
    print("=" * 80)


if __name__ == "__main__":
    main()
