"""ペーパートレード成績レポート。

Issue #8: 勝率・Sharpe・DD・exit_reason内訳。
"""
from __future__ import annotations

import logging
import math
import os
from datetime import datetime
from pathlib import Path

import numpy as np
from dotenv import load_dotenv

from src.data.db import get_connection

logger = logging.getLogger("report")

load_dotenv(Path(__file__).resolve().parent.parent / ".env")

INITIAL_BALANCE = float(os.getenv("INITIAL_BALANCE", "1000000"))


def generate_report(db_path: str | None = None) -> dict:
    """DuckDBからペーパートレード成績を集計。

    Returns:
        dict with keys: total_trades, wins, losses, win_rate, total_pnl,
        max_drawdown_pct, sharpe_ratio, exit_reason_breakdown, monthly_pnl
    """
    con = get_connection(db_path)

    # 全クローズ済みトレード
    trades = con.execute("""
        SELECT pnl, exit_reason, closed_at
        FROM positions WHERE status = 'closed'
        ORDER BY closed_at
    """).fetchall()

    # monthly_pnl
    monthly = con.execute("""
        SELECT year_month, total_pnl, drawdown_pct, is_stopped
        FROM monthly_pnl ORDER BY year_month
    """).fetchall()

    con.close()

    total_trades = len(trades)
    if total_trades == 0:
        return {
            "total_trades": 0,
            "wins": 0,
            "losses": 0,
            "win_rate": 0.0,
            "total_pnl": 0.0,
            "max_drawdown_pct": 0.0,
            "sharpe_ratio": 0.0,
            "exit_reason_breakdown": {},
            "monthly_pnl": [],
            "initial_balance": INITIAL_BALANCE,
        }

    pnls = [t[0] for t in trades]
    reasons = [t[1] for t in trades]

    wins = sum(1 for p in pnls if p > 0)
    losses = sum(1 for p in pnls if p <= 0)
    win_rate = wins / total_trades if total_trades > 0 else 0.0
    total_pnl = sum(pnls)

    # exit_reason内訳
    breakdown = {}
    for r in reasons:
        breakdown[r] = breakdown.get(r, 0) + 1

    # 最大ドローダウン
    cumulative = 0.0
    peak = INITIAL_BALANCE
    max_dd = 0.0
    for p in pnls:
        cumulative += p
        equity = INITIAL_BALANCE + cumulative
        if equity > peak:
            peak = equity
        dd = (peak - equity) / peak if peak > 0 else 0.0
        if dd > max_dd:
            max_dd = dd

    # Sharpe Ratio（日次PnLベース、年率換算）
    sharpe = _calc_sharpe(pnls)

    # monthly_pnl
    monthly_data = [
        {"year_month": m[0], "total_pnl": m[1], "drawdown_pct": m[2], "is_stopped": m[3]}
        for m in monthly
    ]

    return {
        "total_trades": total_trades,
        "wins": wins,
        "losses": losses,
        "win_rate": win_rate,
        "total_pnl": total_pnl,
        "max_drawdown_pct": max_dd * 100,
        "sharpe_ratio": sharpe,
        "exit_reason_breakdown": breakdown,
        "monthly_pnl": monthly_data,
        "initial_balance": INITIAL_BALANCE,
    }


def _calc_sharpe(pnls: list[float], annualize: float = 252.0) -> float:
    """Sharpe Ratio計算（年率換算）。"""
    if len(pnls) < 2:
        return 0.0

    arr = np.array(pnls, dtype=float)
    mean = np.mean(arr)
    std = np.std(arr, ddof=1)

    if std == 0:
        return 0.0

    return float((mean / std) * math.sqrt(annualize))


def print_report(report: dict) -> None:
    """レポートをコンソール出力。"""
    print("=" * 50)
    print("  grove-stock ペーパートレード成績")
    print("=" * 50)
    print(f"  初期資金:     ¥{report['initial_balance']:,.0f}")
    print(f"  総取引数:     {report['total_trades']}")
    print(f"  勝ち:         {report['wins']}")
    print(f"  負け:         {report['losses']}")
    print(f"  勝率:         {report['win_rate']:.1%}")
    print(f"  総損益:       ¥{report['total_pnl']:,.0f}")
    print(f"  最大DD:       {report['max_drawdown_pct']:.1f}%")
    print(f"  Sharpe Ratio: {report['sharpe_ratio']:.2f}")
    print()

    if report["exit_reason_breakdown"]:
        print("  Exit Reason内訳:")
        for reason, count in report["exit_reason_breakdown"].items():
            print(f"    {reason}: {count}")
        print()

    if report["monthly_pnl"]:
        print("  月次損益:")
        for m in report["monthly_pnl"]:
            stopped = " [STOPPED]" if m["is_stopped"] else ""
            print(f"    {m['year_month']}: ¥{m['total_pnl']:,.0f} (DD:{m['drawdown_pct']:.1f}%){stopped}")

    print("=" * 50)


# === CLI ===

def main():
    load_dotenv(Path(__file__).resolve().parent.parent / ".env")
    db_path = str(Path(__file__).resolve().parent.parent / "data" / "grove_stock.duckdb")

    logging.basicConfig(level=logging.INFO)
    report = generate_report(db_path)
    print_report(report)


if __name__ == "__main__":
    main()
