"""shadow_replay の出口ロジックと集約の検証。

ユニット: _simulate_one を手組みの (date, close, deviation) で決定論的に検証。
統合: 実DBで replay() が「3件どころか桁違い」を生むことを assert (Grove要求の証明)。
"""
from __future__ import annotations

from datetime import date, timedelta

import pandas as pd
import pytest

from src.backtest.shadow_replay import (
    LIVE_DB,
    ShadowTrade,
    _simulate_one,
    portfolio_replay,
    replay,
    summarize,
)


def _bars(closes: list[float], devs: list[float], start: date) -> pd.DataFrame:
    return pd.DataFrame({
        "date": [start + timedelta(days=i) for i in range(len(closes))],
        "close": closes,
        "deviation": devs,
    })


def test_stop_loss_exit():
    bars = _bars([100.0, 92.0], [-0.10, -0.10], date(2026, 1, 1))
    t = _simulate_one(bars, date(2026, 1, 1))
    assert t is not None
    assert t.exit_reason == "stop_loss"
    assert t.won is False
    assert t.pnl_pct == pytest.approx(-0.08)


def test_take_profit_exit():
    bars = _bars([100.0, 101.0], [-0.05, 0.0], date(2026, 1, 1))
    t = _simulate_one(bars, date(2026, 1, 1))
    assert t is not None
    assert t.exit_reason == "take_profit"
    assert t.won is True
    assert t.pnl_pct == pytest.approx(0.01)


def test_max_hold_exit():
    closes = [100.0] * 17
    devs = [-0.05] * 17
    bars = _bars(closes, devs, date(2026, 1, 1))
    t = _simulate_one(bars, date(2026, 1, 1))
    assert t is not None
    assert t.exit_reason == "max_hold"
    # pnl 0 → won は False (pnl>0 ではない)
    assert t.won is False


def test_still_open_when_no_exit():
    bars = _bars([100.0, 100.0, 100.0], [-0.05, -0.05, -0.05], date(2026, 1, 1))
    t = _simulate_one(bars, date(2026, 1, 1))
    assert t is not None
    assert t.exit_reason == "still_open"
    assert t.pnl_pct is None
    assert t.won is None


def test_entry_uses_first_bar_on_or_after_signal():
    # シグナル日がデータ先頭より後 → その日以降の最初の終値でエントリー
    bars = _bars([10.0, 20.0, 18.0], [-0.1, -0.1, 0.0], date(2026, 1, 1))
    t = _simulate_one(bars, date(2026, 1, 2))
    assert t is not None
    assert t.entry_price == 20.0  # 1/2 の終値


def test_summarize_winrate_per_threshold():
    trades = [
        ShadowTrade("A", 3, date(2026, 1, 1), 100, date(2026, 1, 2),
                    110, "take_profit", 0.10, 1, True),
        ShadowTrade("B", 3, date(2026, 1, 1), 100, date(2026, 1, 2),
                    93, "stop_loss", -0.07, 1, False),
        ShadowTrade("C", 2, date(2026, 1, 1), 100, None,
                    None, "still_open", None, None, None),
    ]
    out = summarize(trades)
    assert "consensus >= 3" in out
    assert "勝率: 50.0%" in out          # 1勝1敗
    assert "保有中 1" in out             # c=2 の still_open


@pytest.mark.integration
def test_portfolio_replay_runs_and_conserves_sanity():
    if not LIVE_DB.exists():
        pytest.skip("grove_stock.duckdb not present")
    r = portfolio_replay(3, LIVE_DB, initial=1_000_000.0)
    assert r.initial == 1_000_000.0
    assert r.final > 0
    assert r.wins + r.losses == r.n_trades
    assert -1.0 <= r.max_drawdown <= 0.0


@pytest.mark.integration
def test_replay_generates_far_more_than_three():
    """Grove要求の証明: liveは27045スキャン→3取引。再生は桁違いに生む。"""
    if not LIVE_DB.exists():
        pytest.skip("grove_stock.duckdb not present")
    trades = replay(LIVE_DB)
    assert len(trades) > 100, f"expected >>3, got {len(trades)}"
    closed = [t for t in trades if t.pnl_pct is not None]
    assert len(closed) > 50, f"closed trades too few: {len(closed)}"
    for c in (2, 3, 4):
        assert any(t.threshold == c for t in trades), f"no trades at c={c}"
