"""S7: realistic_baseline のコスト数式を決定論で固定。

per-trade のコスト後指標のみ検証（ポートフォリオ maxDD は engine opt-in で測る
方針＝本モジュールは出さない。素朴累積DDの手法アーティファクトを排除済）。
"""
from __future__ import annotations

from src.measurement.realistic_baseline import adjust, _round_trip_cost_pct
from src.data.db import calc_commission


class _T:
    def __init__(self, entry_price, pnl_pct):
        self.entry_price = entry_price
        self.pnl_pct = pnl_pct


class _Res:
    def __init__(self, trades):
        self.trades = trades


def test_round_trip_cost_components():
    # entry=¥2000, raw +5%, ¥100k notional
    comm_pct, slip_pct = _round_trip_cost_pct(2000.0, 0.05, 100_000.0, 10 / 10_000.0)
    shares = 100_000.0 / 2000.0
    exit_price = 2000.0 * 1.05
    expected_comm = (calc_commission(2000.0, shares)
                     + calc_commission(exit_price, shares)) / 100_000.0
    assert comm_pct == expected_comm
    # slippage: (1+r) - (1+r)(1-s)/(1+s) > 0、概ね 2s 近傍
    assert 0 < slip_pct < 0.01


def test_adjust_deducts_cost_and_no_portfolio_maxdd():
    trades = [_T(2000.0, 0.05), _T(2000.0, -0.03), _T(1000.0, 0.10)]
    m = adjust(_Res(trades), slippage_bps=10.0)
    assert m.closed == 3
    # コスト後 avg < raw avg（必ず drag が正）
    assert m.avg_return < m.raw_avg_return
    assert m.cost_drag_per_trade > 0
    # ポートフォリオ maxDD フィールドは存在しない（手法アーティファクト排除）
    assert not hasattr(m, "max_drawdown")


def test_empty_result_safe():
    m = adjust(_Res([]), slippage_bps=10.0)
    assert m.closed == 0 and m.avg_return == 0.0


def test_higher_slippage_more_drag():
    trades = [_T(3000.0, 0.04)] * 5
    lo = adjust(_Res(trades), slippage_bps=5.0).cost_drag_per_trade
    hi = adjust(_Res(trades), slippage_bps=15.0).cost_drag_per_trade
    assert hi > lo > 0
