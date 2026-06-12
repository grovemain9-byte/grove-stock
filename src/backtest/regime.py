"""レジーム分類 + レジーム別バックテスト結果集計。

Phase 1: 定量ルール (日経MA25乖離 + 20日リターン + ボラ)
Phase 2: LLM判断は定量で不十分な場合のみ

Regime:
  - bullish: Nikkei > MA25 by +3%以上
  - bearish: Nikkei < MA25 by -3%以上 OR 過去20日 -5%以上下落
  - ranging: それ以外
"""
from __future__ import annotations

import json
import logging
from datetime import date
from pathlib import Path

import numpy as np
import pandas as pd

from src.backtest.engine import _load_nikkei_bars

logger = logging.getLogger("backtest.regime")

BULL_THRESHOLD = 0.03   # +3% MA乖離
BEAR_THRESHOLD = -0.03  # -3% MA乖離
CRASH_20D = -0.05       # 過去20日 -5%以上下落


def classify_daily_regimes() -> pd.DataFrame:
    """日経225履歴から日次レジームラベルを生成。
    Returns: DataFrame with columns [date, nikkei_close, ma25, ma_dev, ret_20d, regime]
    """
    df = _load_nikkei_bars_full()
    df["ret_20d"] = df["close"].pct_change(20)
    # 3カテゴリ判定
    def regime(r):
        if pd.isna(r["nikkei_dev"]) or pd.isna(r["ret_20d"]):
            return "unknown"
        if r["nikkei_dev"] >= BULL_THRESHOLD:
            return "bullish"
        if r["nikkei_dev"] <= BEAR_THRESHOLD or r["ret_20d"] <= CRASH_20D:
            return "bearish"
        return "ranging"
    df["regime"] = df.apply(regime, axis=1)
    return df


def _load_nikkei_bars_full() -> pd.DataFrame:
    """close+MA25乖離を含むNikkei系列を返す (engine._load_nikkei_barsはdev列のみ)."""
    import yfinance as yf
    t = yf.Ticker("^N225")
    d = t.history(period="5y")
    d = d.reset_index()[["Date","Close"]].rename(columns={"Date":"date","Close":"close"})
    d["date"] = pd.to_datetime(d["date"]).dt.tz_localize(None).dt.date
    d["ma25"] = d["close"].rolling(25).mean()
    d["nikkei_dev"] = (d["close"] - d["ma25"]) / d["ma25"]
    return d


def label_trades_by_regime(trades_json_path: str, regimes: pd.DataFrame) -> pd.DataFrame:
    """/tmp/bt_*.json のtradesにregime列を付与。entry_date時点のregimeを採用。"""
    data = json.loads(Path(trades_json_path).read_text())
    trades = data["trades"]
    reg_map = dict(zip(regimes["date"], regimes["regime"]))
    rows = []
    for t in trades:
        if not t.get("pnl_pct"):
            continue
        entry_str = t["entry"].split(" ")[0]  # strip time part if present
        entry = date.fromisoformat(entry_str)
        reg = reg_map.get(entry, "unknown")
        rows.append({
            "code": t["code"], "entry": entry, "exit": t["exit"],
            "pnl_pct": t["pnl_pct"], "reason": t["reason"],
            "hold_days": t["hold_days"], "consensus": t["consensus"],
            "regime": reg,
        })
    return pd.DataFrame(rows)


def regime_summary(tdf: pd.DataFrame) -> pd.DataFrame:
    """レジーム別の勝率・平均リターン・累積PnL・Sharpe。"""
    rows = []
    for reg, grp in tdf.groupby("regime"):
        pnls = grp["pnl_pct"].values
        wins = pnls > 0
        row = {
            "regime": reg,
            "trades": len(grp),
            "wins": int(wins.sum()),
            "win_rate": float(wins.mean()) if len(pnls) else 0,
            "avg_return": float(pnls.mean()) if len(pnls) else 0,
            "median": float(np.median(pnls)) if len(pnls) else 0,
            "std": float(pnls.std(ddof=1)) if len(pnls) > 1 else 0,
            "total_pnl_sum": float(pnls.sum()),
            "best": float(pnls.max()) if len(pnls) else 0,
            "worst": float(pnls.min()) if len(pnls) else 0,
        }
        row["sharpe_per_trade"] = row["avg_return"] / row["std"] if row["std"] > 0 else 0
        rows.append(row)
    return pd.DataFrame(rows).sort_values("regime")


def regime_day_distribution(regimes: pd.DataFrame, start: str = "2023-05-01", end: str = "2026-04-20") -> pd.DataFrame:
    """期間中のレジーム日数分布 (日次ベース)."""
    mask = (regimes["date"] >= date.fromisoformat(start)) & (regimes["date"] <= date.fromisoformat(end))
    sub = regimes[mask].copy()
    dist = sub["regime"].value_counts().to_frame("days")
    dist["pct"] = dist["days"] / dist["days"].sum() * 100
    return dist
