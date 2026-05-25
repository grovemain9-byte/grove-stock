"""Issue #7: monitor_graph テスト。

受け入れ基準:
- 4条件それぞれでexit_nodeに到達すること
- 条件未成立でENDになること
- PnLとexit_reasonが正しく記録されること
"""
from datetime import date, timedelta
from unittest.mock import patch, AsyncMock

import pandas as pd
import pytest

from src.monitor import build_monitor_graph, run_monitor_cycle, MonitorState, monitor_node
from src.broker.tachibana import MockTachibanaClient
from src.data.db import get_connection


def _make_df(closes: list[float], volumes: list[float] | None = None) -> pd.DataFrame:
    n = len(closes)
    dates = pd.bdate_range(end=pd.Timestamp.now(), periods=n)
    if volumes is None:
        volumes = [100000.0] * n
    return pd.DataFrame({
        "date": dates, "open": closes, "high": [c + 10 for c in closes],
        "low": [c - 10 for c in closes], "close": closes, "volume": volumes,
    })


def _seed_position(db_path, ticker="4519", price=1000.0, shares=100, days_ago=0):
    """テスト用ポジションをDBに挿入。"""
    con = get_connection(db_path)
    entry_date = date.today() - timedelta(days=days_ago)
    con.execute("""
        INSERT INTO positions (ticker, entry_price, shares, entry_date, status)
        VALUES (?, ?, ?, ?, 'open')
    """, [ticker, price, shares, entry_date])
    row = con.execute("SELECT id FROM positions ORDER BY id DESC LIMIT 1").fetchone()
    con.close()
    return row[0]


class TestMonitorGraphStructure:
    def test_graph_builds(self):
        app = build_monitor_graph()
        assert app is not None

    @patch("src.monitor.fetch_daily_ohlcv")
    @patch("src.monitor.fetch_nikkei_change", return_value=0.5)
    @patch("src.monitor.vote_all", new_callable=AsyncMock)
    def test_no_positions_ends(self, mock_vote, mock_nikkei, mock_fetch, tmp_path):
        """ポジションなし → END。"""
        broker = MockTachibanaClient()
        result = run_monitor_cycle(broker=broker, db_path=str(tmp_path / "test.db"))
        assert result["exit_decisions"] == []


class TestStopLoss:
    @patch("src.monitor.fetch_daily_ohlcv")
    @patch("src.monitor.fetch_nikkei_change", return_value=0.5)
    @patch("src.monitor.vote_all", new_callable=AsyncMock)
    def test_stop_loss_triggered(self, mock_vote, mock_nikkei, mock_fetch, tmp_path):
        """エントリー1000円 → 現在920円(-8%) → 損切り。
        令和式: open は entry の -3%以内 にして gap_down_stop を回避し
        intraday で stop_loss発火パターンを検証."""
        db_path = str(tmp_path / "test.db")
        pos_id = _seed_position(db_path, price=1000.0)
        df = _make_df([920.0] * 30)  # close -8%
        df.loc[df.index[-1], "open"] = 980.0  # open -2% (gap_down_stop 未発火)
        mock_fetch.return_value = df
        mock_vote.return_value = {"p1": True, "p2": True, "p3": True, "p4": True, "p5": True, "consensus": 5}

        broker = MockTachibanaClient()
        broker.set_price("4519", 920.0)
        result = run_monitor_cycle(broker=broker, db_path=db_path)

        assert len(result["exit_decisions"]) == 1
        assert result["exit_decisions"][0]["reason"] == "stop_loss"

        con = get_connection(db_path)
        row = con.execute("SELECT status, exit_reason, pnl FROM positions WHERE id = ?", [pos_id]).fetchone()
        con.close()
        assert row[0] == "closed"
        assert row[1] == "stop_loss"
        assert row[2] < 0  # 損失


class TestMaxHold:
    @patch("src.monitor.fetch_daily_ohlcv")
    @patch("src.monitor.fetch_nikkei_change", return_value=0.5)
    @patch("src.monitor.vote_all", new_callable=AsyncMock)
    def test_max_hold_triggered(self, mock_vote, mock_nikkei, mock_fetch, tmp_path):
        """15営業日経過 → 最大保有期間。
        令和式: open close 共に entry の -3%以内 (gap_down/stop_loss/trailing 全部回避)."""
        db_path = str(tmp_path / "test.db")
        pos_id = _seed_position(db_path, price=1000.0, days_ago=22)  # 22日前≈16営業日 >= MAX_HOLD_DAYS(15)
        df = _make_df([980.0] * 30)  # close -2% (損切り-7%未達)
        df.loc[df.index[-1], "open"] = 980.0  # gap_down -3%以内
        mock_fetch.return_value = df
        mock_vote.return_value = {"p1": True, "p2": True, "p3": True, "p4": True, "p5": True, "consensus": 5}

        broker = MockTachibanaClient()
        broker.set_price("4519", 980.0)
        result = run_monitor_cycle(broker=broker, db_path=db_path)

        assert len(result["exit_decisions"]) == 1
        assert result["exit_decisions"][0]["reason"] == "max_hold"


class TestTakeProfit:
    @patch("src.monitor.fetch_daily_ohlcv")
    @patch("src.monitor.fetch_nikkei_change", return_value=0.5)
    @patch("src.monitor.calc_ma25_deviation", return_value=0.025)  # 令和式: +2.5% > threshold +2%
    @patch("src.monitor.vote_all", new_callable=AsyncMock)
    def test_take_profit_triggered(self, mock_vote, mock_dev, mock_nikkei, mock_fetch, tmp_path):
        """令和式 asymmetric_tp: MA25乖離率 >= +2% で利確 (旧 0% から変更)."""
        db_path = str(tmp_path / "test.db")
        pos_id = _seed_position(db_path, price=1000.0, days_ago=2)
        mock_fetch.return_value = _make_df([1050.0] * 30)
        mock_vote.return_value = {"p1": True, "p2": True, "p3": True, "p4": True, "p5": True, "consensus": 5}

        broker = MockTachibanaClient()
        broker.set_price("4519", 1050.0)
        result = run_monitor_cycle(broker=broker, db_path=db_path)

        assert len(result["exit_decisions"]) == 1
        assert result["exit_decisions"][0]["reason"] == "take_profit"

        con = get_connection(db_path)
        row = con.execute("SELECT pnl FROM positions WHERE id = ?", [pos_id]).fetchone()
        con.close()
        assert row[0] > 0  # 利益

    @patch("src.monitor.fetch_daily_ohlcv")
    @patch("src.monitor.fetch_nikkei_change", return_value=0.5)
    @patch("src.monitor.calc_ma25_deviation", return_value=0.01)  # +1%: 旧threshold ならfireするが新+2% threshold では未満
    @patch("src.monitor.vote_all", new_callable=AsyncMock)
    def test_take_profit_below_new_threshold(self, mock_vote, mock_dev, mock_nikkei, mock_fetch, tmp_path):
        """令和式: dev +1% は新threshold (+2%) 未満で take_profit 発火しない."""
        db_path = str(tmp_path / "test.db")
        _seed_position(db_path, price=1000.0, days_ago=2)
        mock_fetch.return_value = _make_df([1010.0] * 30)
        # consensus=5 で signal_reversal も発火しない → exit決定なし (max_hold 未到達)
        mock_vote.return_value = {"p1": True, "p2": True, "p3": True, "p4": True, "p5": True, "consensus": 5}

        broker = MockTachibanaClient()
        broker.set_price("4519", 1010.0)
        result = run_monitor_cycle(broker=broker, db_path=db_path)

        assert len(result["exit_decisions"]) == 0  # 利確 priority変更で +2%未満は持続


# === 令和式 BNF exit層 新規 (2026-05-25 commit 後続) ===

class TestGapDownStop:
    """令和式 gap_down_stop: 寄付きが entry の -3% 以下で即exit (stop_loss -7% の gap補完)."""

    @patch("src.monitor.fetch_daily_ohlcv")
    @patch("src.monitor.fetch_nikkei_change", return_value=0.5)
    @patch("src.monitor.calc_ma25_deviation", return_value=-0.05)
    @patch("src.monitor.vote_all", new_callable=AsyncMock)
    def test_gap_down_fires_at_open_minus_3pct(self, mock_vote, mock_dev, mock_nikkei, mock_fetch, tmp_path):
        """寄付き open=970 vs entry=1000 (-3%) で gap_down_stop 発火."""
        db_path = str(tmp_path / "test.db")
        _seed_position(db_path, price=1000.0, days_ago=1)
        # open は close より下 = gap down
        df = _make_df([975.0] * 30)
        df.loc[df.index[-1], "open"] = 970.0  # -3% gap (entry 1000)
        mock_fetch.return_value = df
        mock_vote.return_value = {"p1": True, "p2": True, "p3": True, "p4": True, "p5": True, "consensus": 5}

        broker = MockTachibanaClient()
        broker.set_price("4519", 975.0)
        result = run_monitor_cycle(broker=broker, db_path=db_path)

        assert len(result["exit_decisions"]) == 1
        assert result["exit_decisions"][0]["reason"] == "gap_down_stop"
        assert result["exit_decisions"][0]["current_price"] == 970.0  # open価格で exit

    @patch("src.monitor.fetch_daily_ohlcv")
    @patch("src.monitor.fetch_nikkei_change", return_value=0.5)
    @patch("src.monitor.calc_ma25_deviation", return_value=-0.05)
    @patch("src.monitor.vote_all", new_callable=AsyncMock)
    def test_gap_down_does_not_fire_at_minus_2pct(self, mock_vote, mock_dev, mock_nikkei, mock_fetch, tmp_path):
        """gap -2% は threshold (-3%) より浅いので未発火."""
        db_path = str(tmp_path / "test.db")
        _seed_position(db_path, price=1000.0, days_ago=1)
        df = _make_df([990.0] * 30)
        df.loc[df.index[-1], "open"] = 980.0  # -2% gap (threshold -3%未満)
        mock_fetch.return_value = df
        mock_vote.return_value = {"p1": True, "p2": True, "p3": True, "p4": True, "p5": True, "consensus": 5}

        broker = MockTachibanaClient()
        broker.set_price("4519", 990.0)
        result = run_monitor_cycle(broker=broker, db_path=db_path)

        assert len(result["exit_decisions"]) == 0  # gap_down 不発火、他も未到達


class TestTrailingStop:
    """令和式 trailing_stop: max +1.5%到達後、peak から -1% drawdown で exit."""

    @patch("src.monitor.fetch_daily_ohlcv")
    @patch("src.monitor.fetch_nikkei_change", return_value=0.5)
    @patch("src.monitor.calc_ma25_deviation", return_value=-0.01)  # take_profit未発火
    @patch("src.monitor.vote_all", new_callable=AsyncMock)
    def test_trailing_fires_after_peak_drawdown(self, mock_vote, mock_dev, mock_nikkei, mock_fetch, tmp_path):
        """max_price_seen=1030 (entry+3%), current=1019 (peak-1.07%) → trailing発火."""
        db_path = str(tmp_path / "test.db")
        pos_id = _seed_position(db_path, price=1000.0, days_ago=1)
        # max_price_seen を事前にセット
        con = get_connection(db_path)
        con.execute("UPDATE positions SET max_price_seen = 1030.0 WHERE id = ?", [pos_id])
        con.close()
        mock_fetch.return_value = _make_df([1019.0] * 30)  # peak 1030 から -1.07% drawdown
        mock_vote.return_value = {"p1": True, "p2": True, "p3": True, "p4": True, "p5": True, "consensus": 5}

        broker = MockTachibanaClient()
        broker.set_price("4519", 1019.0)
        result = run_monitor_cycle(broker=broker, db_path=db_path)

        assert len(result["exit_decisions"]) == 1
        assert result["exit_decisions"][0]["reason"] == "trailing_stop"

    @patch("src.monitor.fetch_daily_ohlcv")
    @patch("src.monitor.fetch_nikkei_change", return_value=0.5)
    @patch("src.monitor.calc_ma25_deviation", return_value=-0.01)
    @patch("src.monitor.vote_all", new_callable=AsyncMock)
    def test_trailing_not_active_below_activation(self, mock_vote, mock_dev, mock_nikkei, mock_fetch, tmp_path):
        """max が entry +1.5% に未到達なら trailing未活性 → 発火しない."""
        db_path = str(tmp_path / "test.db")
        pos_id = _seed_position(db_path, price=1000.0, days_ago=1)
        con = get_connection(db_path)
        con.execute("UPDATE positions SET max_price_seen = 1010.0 WHERE id = ?", [pos_id])  # +1% only
        con.close()
        mock_fetch.return_value = _make_df([995.0] * 30)
        mock_vote.return_value = {"p1": True, "p2": True, "p3": True, "p4": True, "p5": True, "consensus": 5}

        broker = MockTachibanaClient()
        broker.set_price("4519", 995.0)
        result = run_monitor_cycle(broker=broker, db_path=db_path)

        assert len(result["exit_decisions"]) == 0  # trailing 未活性

    @patch("src.monitor.fetch_daily_ohlcv")
    @patch("src.monitor.fetch_nikkei_change", return_value=0.5)
    @patch("src.monitor.calc_ma25_deviation", return_value=-0.01)
    @patch("src.monitor.vote_all", new_callable=AsyncMock)
    def test_max_price_seen_updates_on_new_high(self, mock_vote, mock_dev, mock_nikkei, mock_fetch, tmp_path):
        """current > max_price_seen なら DB更新される."""
        db_path = str(tmp_path / "test.db")
        pos_id = _seed_position(db_path, price=1000.0, days_ago=0)
        mock_fetch.return_value = _make_df([1020.0] * 30)  # new high
        mock_vote.return_value = {"p1": True, "p2": True, "p3": True, "p4": True, "p5": True, "consensus": 5}

        broker = MockTachibanaClient()
        broker.set_price("4519", 1020.0)
        run_monitor_cycle(broker=broker, db_path=db_path)

        con = get_connection(db_path)
        row = con.execute("SELECT max_price_seen FROM positions WHERE id=?", [pos_id]).fetchone()
        con.close()
        assert row[0] == 1020.0  # initialize→entry_price→update→1020


class TestExitPriorityOrder:
    """令和式 priority: gap_down > stop_loss > take_profit > trailing > max_hold > signal_reversal."""

    @patch("src.monitor.fetch_daily_ohlcv")
    @patch("src.monitor.fetch_nikkei_change", return_value=0.5)
    @patch("src.monitor.calc_ma25_deviation", return_value=0.03)  # +3% > take_profit_dev
    @patch("src.monitor.vote_all", new_callable=AsyncMock)
    def test_take_profit_beats_signal_reversal(self, mock_vote, mock_dev, mock_nikkei, mock_fetch, tmp_path):
        """take_profit と signal_reversal が両方trigger可能でも take_profit が先勝ち.

        旧設計の bug: signal_reversal が先勝ちで take_profit 0件発火していた問題の修正検証.
        """
        db_path = str(tmp_path / "test.db")
        _seed_position(db_path, price=1000.0, days_ago=2)
        mock_fetch.return_value = _make_df([1050.0] * 30)
        # consensus=2 で signal_reversal 条件も成立
        mock_vote.return_value = {"p1": False, "p2": True, "p3": False, "p4": True, "p5": False, "consensus": 2}

        broker = MockTachibanaClient()
        broker.set_price("4519", 1050.0)
        result = run_monitor_cycle(broker=broker, db_path=db_path)

        assert len(result["exit_decisions"]) == 1
        assert result["exit_decisions"][0]["reason"] == "take_profit"  # signal_reversal でない


class TestSignalReversal:
    @patch("src.monitor.fetch_daily_ohlcv")
    @patch("src.monitor.fetch_nikkei_change", return_value=0.5)
    @patch("src.monitor.calc_ma25_deviation", return_value=-0.03)  # まだMA25以下
    @patch("src.monitor.vote_all", new_callable=AsyncMock)
    def test_signal_reversal_triggered(self, mock_vote, mock_dev, mock_nikkei, mock_fetch, tmp_path):
        """consensus < 3 → 反転シグナル。"""
        db_path = str(tmp_path / "test.db")
        pos_id = _seed_position(db_path, price=1000.0, days_ago=2)
        mock_fetch.return_value = _make_df([980.0] * 30)  # 微損だが損切りライン未達
        mock_vote.return_value = {"p1": False, "p2": False, "p3": False, "p4": True, "p5": False, "consensus": 1}

        broker = MockTachibanaClient()
        broker.set_price("4519", 980.0)
        result = run_monitor_cycle(broker=broker, db_path=db_path)

        assert len(result["exit_decisions"]) == 1
        assert result["exit_decisions"][0]["reason"] == "signal_reversal"


class TestNoExit:
    @patch("src.monitor.fetch_daily_ohlcv")
    @patch("src.monitor.fetch_nikkei_change", return_value=0.5)
    @patch("src.monitor.calc_ma25_deviation", return_value=-0.03)  # まだMA25以下
    @patch("src.monitor.vote_all", new_callable=AsyncMock)
    def test_no_condition_met(self, mock_vote, mock_dev, mock_nikkei, mock_fetch, tmp_path):
        """全条件未成立 → EXIT無し。"""
        db_path = str(tmp_path / "test.db")
        _seed_position(db_path, price=1000.0, days_ago=1)
        mock_fetch.return_value = _make_df([990.0] * 30)  # -1%（損切り-5%未満）
        mock_vote.return_value = {"p1": True, "p2": True, "p3": True, "p4": True, "p5": False, "consensus": 4}

        broker = MockTachibanaClient()
        broker.set_price("4519", 990.0)
        result = run_monitor_cycle(broker=broker, db_path=db_path)

        assert len(result["exit_decisions"]) == 0


class TestMonthlyPnl:
    @patch("src.monitor.fetch_daily_ohlcv")
    @patch("src.monitor.fetch_nikkei_change", return_value=0.5)
    @patch("src.monitor.vote_all", new_callable=AsyncMock)
    def test_monthly_pnl_updated(self, mock_vote, mock_nikkei, mock_fetch, tmp_path):
        """決済後にmonthly_pnlが更新される。"""
        db_path = str(tmp_path / "test.db")
        _seed_position(db_path, price=1000.0)
        mock_fetch.return_value = _make_df([930.0] * 30)  # -7% → 損切り
        mock_vote.return_value = {"p1": True, "p2": True, "p3": True, "p4": True, "p5": True, "consensus": 5}

        broker = MockTachibanaClient()
        broker.set_price("4519", 930.0)
        run_monitor_cycle(broker=broker, db_path=db_path)

        from datetime import datetime
        ym = datetime.now().strftime("%Y-%m")
        con = get_connection(db_path)
        row = con.execute("SELECT total_pnl FROM monthly_pnl WHERE year_month = ?", [ym]).fetchone()
        con.close()
        assert row is not None
        assert row[0] < 0  # 損失が記録
