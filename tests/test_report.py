"""Issue #8: レポートテスト。

受け入れ基準:
- Sharpe Ratioが正しく計算されること
- 勝率が正しく計算されること
"""
import math
import pytest

from src.report import generate_report, _calc_sharpe, INITIAL_BALANCE
from src.data.db import get_connection


def _seed_trades(db_path: str, trades: list[tuple]) -> None:
    """テスト用トレードを挿入。trades: [(pnl, exit_reason), ...]"""
    con = get_connection(db_path)
    for i, (pnl, reason) in enumerate(trades):
        con.execute("""
            INSERT INTO positions (ticker, entry_price, shares, entry_date, status,
                                   exit_price, exit_reason, pnl, closed_at)
            VALUES (?, 1000.0, 100, '2026-04-01', 'closed', ?, ?, ?, '2026-04-05')
        """, [f"TEST{i}", 1000.0 + pnl / 100, reason, pnl])
    con.close()


class TestGenerateReport:
    def test_empty_db(self, tmp_path):
        report = generate_report(str(tmp_path / "test.db"))
        assert report["total_trades"] == 0
        assert report["win_rate"] == 0.0
        assert report["sharpe_ratio"] == 0.0

    def test_all_wins(self, tmp_path):
        db_path = str(tmp_path / "test.db")
        _seed_trades(db_path, [
            (10000, "take_profit"),
            (5000, "take_profit"),
            (8000, "take_profit"),
        ])
        report = generate_report(db_path)
        assert report["total_trades"] == 3
        assert report["wins"] == 3
        assert report["losses"] == 0
        assert report["win_rate"] == 1.0
        assert report["total_pnl"] == 23000

    def test_mixed_results(self, tmp_path):
        db_path = str(tmp_path / "test.db")
        _seed_trades(db_path, [
            (10000, "take_profit"),
            (-5000, "stop_loss"),
            (8000, "take_profit"),
            (-3000, "stop_loss"),
            (15000, "max_hold"),
        ])
        report = generate_report(db_path)
        assert report["total_trades"] == 5
        assert report["wins"] == 3
        assert report["losses"] == 2
        assert report["win_rate"] == pytest.approx(0.6)
        assert report["total_pnl"] == 25000

    def test_exit_reason_breakdown(self, tmp_path):
        db_path = str(tmp_path / "test.db")
        _seed_trades(db_path, [
            (10000, "take_profit"),
            (-5000, "stop_loss"),
            (3000, "max_hold"),
            (-2000, "signal_reversal"),
            (8000, "take_profit"),
        ])
        report = generate_report(db_path)
        assert report["exit_reason_breakdown"]["take_profit"] == 2
        assert report["exit_reason_breakdown"]["stop_loss"] == 1
        assert report["exit_reason_breakdown"]["max_hold"] == 1
        assert report["exit_reason_breakdown"]["signal_reversal"] == 1

    def test_max_drawdown(self, tmp_path):
        db_path = str(tmp_path / "test.db")
        # 10000勝ち → -30000負け → 5000勝ち
        # peak=1010000, trough=1010000-30000=980000 → DD=30000/1010000≒2.97%
        _seed_trades(db_path, [
            (10000, "take_profit"),
            (-30000, "stop_loss"),
            (5000, "take_profit"),
        ])
        report = generate_report(db_path)
        assert report["max_drawdown_pct"] > 2.0
        assert report["max_drawdown_pct"] < 4.0


class TestSharpeRatio:
    def test_positive_sharpe(self):
        # 一貫して利益
        pnls = [1000, 1200, 800, 1100, 900, 1300, 1000]
        sharpe = _calc_sharpe(pnls)
        assert sharpe > 0

    def test_negative_sharpe(self):
        # 一貫して損失
        pnls = [-1000, -1200, -800, -1100, -900]
        sharpe = _calc_sharpe(pnls)
        assert sharpe < 0

    def test_zero_variance(self):
        # 全部同じ → std=0 → sharpe=0
        pnls = [1000, 1000, 1000]
        sharpe = _calc_sharpe(pnls)
        assert sharpe == 0.0

    def test_single_trade(self):
        sharpe = _calc_sharpe([5000])
        assert sharpe == 0.0

    def test_empty(self):
        sharpe = _calc_sharpe([])
        assert sharpe == 0.0


class TestInitialBalance:
    def test_default_is_million(self):
        assert INITIAL_BALANCE == 1_000_000.0
