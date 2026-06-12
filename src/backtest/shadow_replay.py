"""Shadow Replay: 記録済みscansシグナルを過去実価格に再生して勝敗データを生成する。

なぜ存在するか:
    liveボットは consensus>=3 の買いシグナルを978回出したが positions は3件のみ
    (実行パイプライン不全)。勝敗データが貯まらず「どの厳しさ(consensus閾値)が
    勝てるか」を検証できない。本モジュールは *既に scans に記録済みの* シグナルを
    過去価格にぶつけ、live を一切変更せず c=2/3/4 別の win/loss を即座に生成する。

設計判断 (平易な説明):
    - 出口ルールは戦略核心。自前で書かず src/backtest/engine.py の定数
      (STOP_LOSS, MAX_HOLD_DAYS) と take_profit条件 (deviation>=0) を流用。
    - 1銘柄1日に複数の場中スキャンが記録されるため (ticker, date) に集約し
      その日の最大consensusを採用 = 「その日エントリーしたか」の現実的モデル。
    - 各シグナルは独立した仮想エントリー (日跨ぎの重複を許す)。
      これは戦略の資金管理ではなく「シグナル品質」を測るための単純化。
    - キャッシュ末尾までに出口未到達のものは exit_reason='still_open' とし
      勝率計算から除外 (engineと同じ扱い)。
"""
from __future__ import annotations

import argparse
import logging
from dataclasses import dataclass
from datetime import date
from pathlib import Path

import duckdb
import pandas as pd

from src.backtest.engine import MAX_HOLD_DAYS, STOP_LOSS, _compute_indicators
from src.data.cache import load_bars

logger = logging.getLogger("backtest.shadow_replay")

LIVE_DB = Path(__file__).resolve().parent.parent.parent / "data" / "grove_stock.duckdb"
THRESHOLDS: tuple[int, ...] = (2, 3, 4)


@dataclass
class ShadowTrade:
    ticker: str
    threshold: int
    signal_date: date
    entry_price: float
    exit_date: date | None
    exit_price: float | None
    exit_reason: str
    pnl_pct: float | None
    hold_days: int | None
    won: bool | None


def _daily_signals(db_path: Path) -> pd.DataFrame:
    """scans を (ticker, date) に集約。その日の最大consensusを採用。

    Returns: columns [ticker, sig_date, consensus]
    """
    con = duckdb.connect(str(db_path), read_only=True)
    df = con.execute(
        """
        SELECT ticker,
               CAST(scanned_at AS DATE) AS sig_date,
               MAX(consensus)           AS consensus
        FROM scans
        WHERE consensus >= 2
        GROUP BY ticker, CAST(scanned_at AS DATE)
        ORDER BY ticker, sig_date
        """
    ).fetchdf()
    con.close()
    df["sig_date"] = pd.to_datetime(df["sig_date"]).dt.date
    return df


def _simulate_one(bars_ind: pd.DataFrame, sig_date: date) -> ShadowTrade | None:
    """指標付きbarsとシグナル日から、engineと同じ出口ルールで1トレードを再生。

    entry: シグナル日 (なければ直後の営業日) の終値。
    exit:  翌営業日以降に stop_loss / max_hold / take_profit(deviation>=0)。
    """
    bars = bars_ind[bars_ind["date"] >= sig_date].reset_index(drop=True)
    if bars.empty or len(bars) < 2:
        return None
    entry_row = bars.iloc[0]
    if pd.isna(entry_row["close"]):
        return None
    entry_price = float(entry_row["close"])

    for i in range(1, len(bars)):
        row = bars.iloc[i]
        cur = row["close"]
        if pd.isna(cur):
            continue
        hold = i  # 営業日数 (engineと同じ index 距離)
        pnl = (cur - entry_price) / entry_price
        reason: str | None = None
        if pnl <= STOP_LOSS:
            reason = "stop_loss"
        elif hold >= MAX_HOLD_DAYS:
            reason = "max_hold"
        elif not pd.isna(row["deviation"]) and row["deviation"] >= 0:
            reason = "take_profit"
        if reason:
            return ShadowTrade(
                ticker="", threshold=0, signal_date=sig_date,
                entry_price=entry_price, exit_date=row["date"],
                exit_price=float(cur), exit_reason=reason,
                pnl_pct=float(pnl), hold_days=hold, won=bool(pnl > 0),
            )

    # キャッシュ末尾まで出口未到達
    return ShadowTrade(
        ticker="", threshold=0, signal_date=sig_date,
        entry_price=entry_price, exit_date=None, exit_price=None,
        exit_reason="still_open", pnl_pct=None, hold_days=None, won=None,
    )


def replay(db_path: Path = LIVE_DB) -> list[ShadowTrade]:
    """記録済み全シグナルを c=2/3/4 別に再生してShadowTradeのリストを返す。"""
    signals = _daily_signals(db_path)
    tickers = signals["ticker"].unique().tolist()
    logger.info("replaying signals: %d ticker, %d (ticker,date) rows",
                len(tickers), len(signals))

    out: list[ShadowTrade] = []
    skipped_no_bars = 0
    for ticker in tickers:
        raw = load_bars(ticker)
        if raw is None or len(raw) < 30:
            skipped_no_bars += 1
            continue
        raw = raw.copy()
        raw["date"] = pd.to_datetime(raw["date"]).dt.date
        ind = _compute_indicators(raw)
        tsig = signals[signals["ticker"] == ticker]
        for _, s in tsig.iterrows():
            base = _simulate_one(ind, s["sig_date"])
            if base is None:
                continue
            for c in THRESHOLDS:
                if s["consensus"] >= c:
                    out.append(ShadowTrade(
                        ticker=ticker, threshold=c, signal_date=base.signal_date,
                        entry_price=base.entry_price, exit_date=base.exit_date,
                        exit_price=base.exit_price, exit_reason=base.exit_reason,
                        pnl_pct=base.pnl_pct, hold_days=base.hold_days, won=base.won,
                    ))
    logger.info("generated %d shadow trades (skipped %d tickers w/o cached bars)",
                len(out), skipped_no_bars)
    return out


def persist(trades: list[ShadowTrade], db_path: Path = LIVE_DB) -> None:
    """shadow_trades テーブルへ保存 (非破壊: 新規テーブル、毎回入替で冪等)。"""
    con = duckdb.connect(str(db_path))
    con.execute(
        """
        CREATE TABLE IF NOT EXISTS shadow_trades (
            ticker VARCHAR, threshold INTEGER, signal_date DATE,
            entry_price DOUBLE, exit_date DATE, exit_price DOUBLE,
            exit_reason VARCHAR, pnl_pct DOUBLE, hold_days INTEGER, won BOOLEAN,
            generated_at TIMESTAMP DEFAULT now()
        )
        """
    )
    con.execute("DELETE FROM shadow_trades")
    if trades:
        df = pd.DataFrame([{
            "ticker": t.ticker, "threshold": t.threshold,
            "signal_date": t.signal_date, "entry_price": t.entry_price,
            "exit_date": t.exit_date, "exit_price": t.exit_price,
            "exit_reason": t.exit_reason, "pnl_pct": t.pnl_pct,
            "hold_days": t.hold_days, "won": t.won,
        } for t in trades])
        con.register("_tmp", df)
        con.execute(
            "INSERT INTO shadow_trades "
            "(ticker,threshold,signal_date,entry_price,exit_date,exit_price,"
            "exit_reason,pnl_pct,hold_days,won) "
            "SELECT ticker,threshold,signal_date,entry_price,exit_date,"
            "exit_price,exit_reason,pnl_pct,hold_days,won FROM _tmp"
        )
    con.close()


def summarize(trades: list[ShadowTrade]) -> str:
    """c=2/3/4 別の勝敗サマリーを文字列で返す。"""
    lines = ["=" * 56, "SHADOW REPLAY — consensus閾値別 勝敗データ", "=" * 56]
    for c in THRESHOLDS:
        grp = [t for t in trades if t.threshold == c]
        closed = [t for t in grp if t.pnl_pct is not None]
        still = len(grp) - len(closed)
        lines.append(f"\n[ consensus >= {c} ]  シグナル {len(grp)}件 "
                     f"(決済済 {len(closed)} / 保有中 {still})")
        if not closed:
            lines.append("  決済済データなし")
            continue
        wins = [t for t in closed if t.won]
        pnls = [t.pnl_pct for t in closed]
        reasons: dict[str, int] = {}
        for t in closed:
            reasons[t.exit_reason] = reasons.get(t.exit_reason, 0) + 1
        lines.append(f"  勝率: {len(wins)/len(closed)*100:.1f}%  "
                     f"({len(wins)}勝 {len(closed)-len(wins)}敗)")
        lines.append(f"  平均リターン/取引: {sum(pnls)/len(pnls)*100:+.2f}%")
        lines.append(f"  最良/最悪: {max(pnls)*100:+.1f}% / {min(pnls)*100:+.1f}%")
        lines.append(f"  出口内訳: {reasons}")
    return "\n".join(lines)


@dataclass
class PortfolioResult:
    threshold: int
    initial: float
    final: float
    n_trades: int
    wins: int
    losses: int
    max_drawdown: float
    peak: float


def portfolio_replay(
    threshold: int,
    db_path: Path = LIVE_DB,
    initial: float = 1_000_000.0,
    max_concurrent: int = 7,
) -> PortfolioResult:
    """「初期initial円でシグナルをペーパー運用していたら」を時系列で再現。

    実paramsに準拠: kelly_size(consensus→残高%) で建玉、同時保有上限、
    engine.py と同じ出口ルール。c<3 は kelly_size が0を返すため
    floor配分7%で建てる(あくまで仮想・liveは c>=3 のみ建玉)。
    """
    from src.kelly import kelly_size

    sig = _daily_signals(db_path)
    sig = sig[sig["consensus"] >= threshold].copy()
    if sig.empty:
        return PortfolioResult(threshold, initial, initial, 0, 0, 0, 0.0, initial)

    tickers = sig["ticker"].unique().tolist()
    ind: dict[str, pd.DataFrame] = {}
    for t in tickers:
        raw = load_bars(t)
        if raw is None or len(raw) < 30:
            continue
        raw = raw.copy()
        raw["date"] = pd.to_datetime(raw["date"]).dt.date
        d = _compute_indicators(raw).set_index("date")
        ind[t] = d

    sig = sig[sig["ticker"].isin(ind)]
    entry_dates = sorted(sig["sig_date"].unique())
    all_dates = sorted({dt for d in ind.values() for dt in d.index})
    all_dates = [d for d in all_dates if d >= entry_dates[0]]

    cash = initial
    open_pos: dict[str, dict] = {}  # ticker -> {entry_price, shares, entry_idx}
    wins = losses = n = 0
    peak = initial
    max_dd = 0.0

    for cur in all_dates:
        # mark-to-market equity
        mtm = cash
        for tk, p in open_pos.items():
            d = ind[tk]
            if cur in d.index:
                mtm += p["shares"] * float(d.loc[cur, "close"])
        peak = max(peak, mtm)
        if peak > 0:
            max_dd = min(max_dd, (mtm - peak) / peak)

        # exits
        for tk in list(open_pos.keys()):
            p = open_pos[tk]
            d = ind[tk]
            if cur not in d.index:
                continue
            row = d.loc[cur]
            cur_px = float(row["close"])
            hold = d.index.get_loc(cur) - p["entry_idx"]
            if hold <= 0:
                continue
            pnl = (cur_px - p["entry_price"]) / p["entry_price"]
            reason = None
            if pnl <= STOP_LOSS:
                reason = "stop_loss"
            elif hold >= MAX_HOLD_DAYS:
                reason = "max_hold"
            elif not pd.isna(row["deviation"]) and row["deviation"] >= 0:
                reason = "take_profit"
            if reason:
                cash += p["shares"] * cur_px
                n += 1
                if pnl > 0:
                    wins += 1
                else:
                    losses += 1
                del open_pos[tk]

        # entries (このsig_dateに出たシグナル)
        todays = sig[sig["sig_date"] == cur]
        for _, s in todays.iterrows():
            tk = s["ticker"]
            if tk in open_pos or len(open_pos) >= max_concurrent:
                continue
            d = ind[tk]
            if cur not in d.index:
                continue
            px = float(d.loc[cur, "close"])
            c = int(s["consensus"])
            shares = kelly_size(c, cash, px) if c >= 3 else (
                int(cash * 0.07 / px / 100) * 100)
            if shares <= 0 or shares * px > cash:
                continue
            cash -= shares * px
            open_pos[tk] = {
                "entry_price": px, "shares": shares,
                "entry_idx": d.index.get_loc(cur),
            }

    # 残ポジションは最終値で清算（評価）
    for tk, p in open_pos.items():
        d = ind[tk]
        last_px = float(d.iloc[-1]["close"])
        cash += p["shares"] * last_px

    return PortfolioResult(
        threshold=threshold, initial=initial, final=cash,
        n_trades=n, wins=wins, losses=losses,
        max_drawdown=max_dd, peak=peak,
    )


def summarize_portfolio(results: list[PortfolioResult]) -> str:
    lines = ["", "=" * 56,
             "100万円スタートで再現したら（実params: kelly/同時保有7）",
             "=" * 56]
    for r in results:
        ret = (r.final - r.initial) / r.initial * 100
        wr = r.wins / (r.wins + r.losses) * 100 if (r.wins + r.losses) else 0.0
        lines.append(
            f"\n[ consensus >= {r.threshold} ]"
            f"\n  最終資産: ¥{r.final:,.0f}  (損益 {ret:+.1f}% / "
            f"開始 ¥{r.initial:,.0f})"
            f"\n  決済トレード: {r.n_trades}件  勝率 {wr:.1f}% "
            f"({r.wins}勝 {r.losses}敗)"
            f"\n  最大ドローダウン: {r.max_drawdown*100:.1f}%"
        )
    lines.append("\n※ 期間 2026-04-21〜05-14（約3.5週間）の記録済シグナル基準")
    return "\n".join(lines)


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    p = argparse.ArgumentParser(description="記録済シグナルを再生し勝敗データ生成")
    p.add_argument("--no-persist", action="store_true",
                   help="DB保存せずサマリーのみ")
    p.add_argument("--initial", type=float, default=1_000_000.0,
                   help="ポートフォリオ初期資産（円）")
    args = p.parse_args()

    trades = replay()
    print(summarize(trades))
    pf = [portfolio_replay(c, initial=args.initial) for c in THRESHOLDS]
    print(summarize_portfolio(pf))
    if not args.no_persist:
        persist(trades)
        logger.info("persisted %d rows to shadow_trades", len(trades))


if __name__ == "__main__":
    main()
