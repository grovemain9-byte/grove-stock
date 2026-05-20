"""Issue #6: scan_graph テスト。

受け入れ基準:
- consensus >= 3でexecution_nodeまで到達すること
- consensus < 3でENDになること
- monthly_dd_exceededでENDになること
"""
import asyncio
from unittest.mock import patch, MagicMock, AsyncMock
from datetime import datetime

import pandas as pd
import pytest

import duckdb

from src.main import build_scan_graph, run_scan_cycle, ScanState, kelly_node
from src.kelly import kelly_size, CONSENSUS_ALLOCATION
from src.broker.tachibana import MockTachibanaClient
from src.data.db import get_connection


# --- ヘルパー ---

def _make_df(closes: list[float], volumes: list[float] | None = None) -> pd.DataFrame:
    n = len(closes)
    dates = pd.bdate_range(end=pd.Timestamp.now(), periods=n)
    if volumes is None:
        volumes = [100000.0] * n
    return pd.DataFrame({
        "date": dates,
        "open": closes,
        "high": [c + 10 for c in closes],
        "low": [c - 10 for c in closes],
        "close": closes,
        "volume": volumes,
    })


# === Kelly ===

class TestKelly:
    def test_consensus_3(self):
        shares = kelly_size(3, 1_000_000, 1000.0)
        # 10% of 1M = 100K → 100K/1000 = 100 shares
        assert shares == 100

    def test_consensus_4(self):
        shares = kelly_size(4, 1_000_000, 1000.0)
        # 15% of 1M = 150K → 150 shares
        assert shares == 100  # 100株単位で丸め

    def test_consensus_5(self):
        shares = kelly_size(5, 1_000_000, 1000.0)
        # 20% of 1M = 200K → 200 shares
        assert shares == 200

    def test_consensus_below_3_returns_0(self):
        assert kelly_size(2, 1_000_000, 1000.0) == 0
        assert kelly_size(0, 1_000_000, 1000.0) == 0

    def test_zero_balance(self):
        assert kelly_size(3, 0, 1000.0) == 0

    def test_expensive_stock(self):
        # 1M * 10% = 100K, price=50000 → 100K/50000 = 2 → 丸めて0
        assert kelly_size(3, 1_000_000, 50000.0) == 0

    def test_100_unit_rounding(self):
        # 1M * 10% = 100K, price=3000 → 33.3 → 丸めて0 (33*100=3300未満)
        shares = kelly_size(3, 1_000_000, 3000.0)
        assert shares % 100 == 0


# === scan_graph ===

class TestScanGraphStructure:
    def test_graph_builds(self):
        app = build_scan_graph()
        assert app is not None

    @patch("src.main.fetch_daily_ohlcv")
    @patch("src.main.fetch_nikkei_change", return_value=0.5)
    @patch("src.main.vote_all", new_callable=AsyncMock)
    def test_no_signal_ends_early(self, mock_vote, mock_nikkei, mock_fetch, tmp_path):
        """consensus < 3 → ENDで止まる。"""
        mock_fetch.return_value = _make_df([1000.0] * 30)
        mock_vote.return_value = {
            "p1": False, "p2": False, "p3": False, "p4": True, "p5": False, "consensus": 1
        }

        broker = MockTachibanaClient()
        result = run_scan_cycle(broker=broker, db_path=str(tmp_path / "test.db"))
        assert result["position_size"] == {}

    @patch("src.main.fetch_daily_ohlcv")
    @patch("src.main.fetch_nikkei_change", return_value=0.5)
    @patch("src.main.vote_all", new_callable=AsyncMock)
    def test_dd_exceeded_ends_early(self, mock_vote, mock_nikkei, mock_fetch, tmp_path):
        """monthly_dd_exceeded → ENDで止まる。"""
        mock_fetch.return_value = _make_df([1000.0] * 30)
        mock_vote.return_value = {
            "p1": True, "p2": True, "p3": True, "p4": True, "p5": True, "consensus": 5
        }

        broker = MockTachibanaClient()
        result = run_scan_cycle(
            broker=broker,
            db_path=str(tmp_path / "test.db"),
            monthly_dd_exceeded=True,
        )
        assert result["position_size"] == {}

    @patch("src.main.fetch_nikkei_ma25_deviation", return_value=-0.05)
    @patch("src.main.fetch_daily_ohlcv")
    @patch("src.main.fetch_nikkei_change", return_value=0.5)
    @patch("src.main.vote_all", new_callable=AsyncMock)
    def test_signal_reaches_execution(self, mock_vote, mock_nikkei, mock_fetch, mock_nk_ma25, tmp_path):
        """consensus >= 4 → bearishレジーム(-5%) → execution_nodeまで到達。"""
        mock_fetch.return_value = _make_df([1000.0] * 30)
        mock_vote.return_value = {
            "p1": True, "p2": True, "p3": True, "p4": True, "p5": False, "consensus": 4
        }

        broker = MockTachibanaClient(initial_balance=1_000_000.0)
        broker.set_price("4519", 1000.0)
        # 全銘柄に同じ価格設定
        for t in ["4568", "4523", "7203", "7267", "7011", "8306", "8316",
                   "9984", "6758", "6861", "9433", "2914"]:
            broker.set_price(t, 1000.0)

        db_path = str(tmp_path / "test.db")
        result = run_scan_cycle(broker=broker, db_path=db_path)

        # position_sizeが1銘柄以上
        assert len(result["position_size"]) > 0

    @patch("src.main.fetch_nikkei_ma25_deviation", return_value=-0.05)
    @patch("src.main.fetch_daily_ohlcv")
    @patch("src.main.fetch_nikkei_change", return_value=0.5)
    @patch("src.main.vote_all", new_callable=AsyncMock)
    def test_positions_recorded_in_db(self, mock_vote, mock_nikkei, mock_fetch, mock_nk_ma25, tmp_path):
        """execution後にpositionsテーブルに記録。"""
        mock_fetch.return_value = _make_df([1000.0] * 30)
        mock_vote.return_value = {
            "p1": True, "p2": True, "p3": True, "p4": True, "p5": False, "consensus": 4
        }

        broker = MockTachibanaClient(initial_balance=1_000_000.0)
        for t in ["4519", "4568", "4523", "7203", "7267", "7011", "8306", "8316",
                   "9984", "6758", "6861", "9433", "2914"]:
            broker.set_price(t, 1000.0)

        db_path = str(tmp_path / "test.db")
        run_scan_cycle(broker=broker, db_path=db_path)

        con = get_connection(db_path)
        count = con.execute("SELECT COUNT(*) FROM positions WHERE status = 'open'").fetchone()[0]
        con.close()
        assert count > 0

    @patch("src.main.fetch_daily_ohlcv")
    @patch("src.main.fetch_nikkei_change", return_value=0.5)
    @patch("src.main.vote_all", new_callable=AsyncMock)
    def test_duplicate_position_prevented(self, mock_vote, mock_nikkei, mock_fetch, tmp_path):
        """同銘柄の重複ポジションは作らない。"""
        mock_fetch.return_value = _make_df([1000.0] * 30)
        mock_vote.return_value = {
            "p1": True, "p2": True, "p3": True, "p4": True, "p5": False, "consensus": 4
        }

        broker = MockTachibanaClient(initial_balance=5_000_000.0)
        for t in ["4519", "4568", "4523", "7203", "7267", "7011", "8306", "8316",
                   "9984", "6758", "6861", "9433", "2914"]:
            broker.set_price(t, 1000.0)

        db_path = str(tmp_path / "test.db")

        # 1回目
        run_scan_cycle(broker=broker, db_path=db_path)
        con = get_connection(db_path)
        count1 = con.execute("SELECT COUNT(*) FROM positions WHERE status = 'open'").fetchone()[0]
        con.close()

        # 2回目（同じ銘柄は重複防止）
        run_scan_cycle(broker=broker, db_path=db_path)
        con = get_connection(db_path)
        count2 = con.execute("SELECT COUNT(*) FROM positions WHERE status = 'open'").fetchone()[0]
        con.close()

        assert count2 == count1


# === decision_shadow 配線テスト (2026-05-20) ===

class TestDecisionShadowWiring:
    """kelly_node が go/pass を decision_shadow に記録することを検証。

    背景: regime_filter LIVE A/B（hypothesis_shadow id=7, GO/applied=false）の
    A/B評価には kelly_node の判定を全部 DB に残す必要がある。配線抜けで
    decision_shadow が 0 records になっていた問題（2026-05-20）を防ぐ。
    """

    def _votes_p4_true(self, ticker: str) -> dict:
        return {ticker: {"p1": True, "p2": True, "p3": True, "p4": True,
                          "p5": False, "consensus": 4, "dev": -0.06}}

    def _votes_p4_false(self, ticker: str) -> dict:
        return {ticker: {"p1": True, "p2": True, "p3": True, "p4": False,
                          "p5": False, "consensus": 4, "dev": -0.06}}

    def test_go_recorded_for_control_book(self, tmp_path):
        """control (regime_filter=False) で shares>0 → decision='go' 1行。"""
        db_path = str(tmp_path / "test.db")
        state: ScanState = {
            "market_data": {"4519": _make_df([1000.0] * 30)},
            "votes": self._votes_p4_true("4519"),
            "position_size": {}, "errors": [], "db_path": db_path, "broker": None,
            "book": "p1m", "book_equity": 1_000_000.0,
            "book_free_cash": 1_000_000.0, "book_flex": False,
            "book_regime_filter": False, "book_open_tickers": set(),
        }
        kelly_node(state)

        con = duckdb.connect(db_path, read_only=True)
        rows = con.execute(
            "SELECT ticker, book, decision, consensus, edge, council_reason "
            "FROM decision_shadow"
        ).fetchall()
        con.close()
        assert len(rows) == 1
        ticker, book, decision, consensus, edge, reason = rows[0]
        assert ticker == "4519"
        assert book == "p1m"
        assert decision == "go"
        assert consensus == 4
        assert edge == pytest.approx(-0.06)
        assert reason == "kelly_ok"

    def test_regime_filter_pass_recorded_for_treatment(self, tmp_path):
        """treatment (regime_filter=True) + p4=False → decision='pass' + skip理由。"""
        db_path = str(tmp_path / "test.db")
        state: ScanState = {
            "market_data": {"4519": _make_df([1000.0] * 30)},
            "votes": self._votes_p4_false("4519"),
            "position_size": {}, "errors": [], "db_path": db_path, "broker": None,
            "book": "p5m", "book_equity": 5_000_000.0,
            "book_free_cash": 5_000_000.0, "book_flex": False,
            "book_regime_filter": True, "book_open_tickers": set(),
        }
        kelly_node(state)

        con = duckdb.connect(db_path, read_only=True)
        rows = con.execute(
            "SELECT ticker, book, decision, council_reason "
            "FROM decision_shadow"
        ).fetchall()
        con.close()
        assert len(rows) == 1
        ticker, book, decision, reason = rows[0]
        assert ticker == "4519"
        assert book == "p5m"
        assert decision == "pass"
        assert reason == "regime_filter_skip:p4=False"

    def test_multibook_same_ticker_separate_rows(self, tmp_path):
        """同 ticker が control go + treatment pass で 2 行記録される。"""
        db_path = str(tmp_path / "test.db")
        votes = self._votes_p4_false("4519")  # p4=False
        md = {"4519": _make_df([1000.0] * 30)}

        # control: regime_filter=False → go
        kelly_node({
            "market_data": md, "votes": votes, "position_size": {}, "errors": [],
            "db_path": db_path, "broker": None,
            "book": "p1m", "book_equity": 1_000_000.0,
            "book_free_cash": 1_000_000.0, "book_flex": False,
            "book_regime_filter": False, "book_open_tickers": set(),
        })
        # treatment: regime_filter=True + p4=False → pass
        kelly_node({
            "market_data": md, "votes": votes, "position_size": {}, "errors": [],
            "db_path": db_path, "broker": None,
            "book": "p5m", "book_equity": 5_000_000.0,
            "book_free_cash": 5_000_000.0, "book_flex": False,
            "book_regime_filter": True, "book_open_tickers": set(),
        })

        con = duckdb.connect(db_path, read_only=True)
        rows = con.execute(
            "SELECT book, decision, council_reason FROM decision_shadow "
            "WHERE ticker = '4519' ORDER BY book"
        ).fetchall()
        con.close()
        assert len(rows) == 2
        assert rows[0] == ("p1m", "go", "kelly_ok")
        assert rows[1] == ("p5m", "pass", "regime_filter_skip:p4=False")

    def test_max_concurrent_full_records_pass(self, tmp_path):
        """既存 open positions が枠を全部埋めている場合、consensus≥3 の全 ticker を
        max_concurrent_full reason で pass 記録する（A/B 評価のため）。"""
        from src.main import MAX_CONCURRENT_POSITIONS

        db_path = str(tmp_path / "test.db")
        # 7 枠を架空 ticker で埋める
        existing = {f"T{i:04d}" for i in range(MAX_CONCURRENT_POSITIONS)}
        state: ScanState = {
            "market_data": {"4519": _make_df([1000.0] * 30)},
            "votes": self._votes_p4_true("4519"),
            "position_size": {}, "errors": [], "db_path": db_path, "broker": None,
            "book": "p1m", "book_equity": 1_000_000.0,
            "book_free_cash": 1_000_000.0, "book_flex": False,
            "book_regime_filter": False, "book_open_tickers": existing,
        }
        kelly_node(state)

        con = duckdb.connect(db_path, read_only=True)
        rows = con.execute(
            "SELECT ticker, decision, council_reason FROM decision_shadow"
        ).fetchall()
        con.close()
        assert len(rows) == 1
        assert rows[0] == ("4519", "pass", "max_concurrent_full")

    def test_consensus_below_threshold_not_recorded(self, tmp_path):
        """consensus<3 は記録されない（A/B評価対象外・DB肥大化防止）。

        記録対象外のため kelly_node は record_proposal を一度も呼ばず、
        DB ファイル自体が作られない。schema を先に初期化してから count を取る。
        """
        from src.measurement.decision_shadow import _connect

        db_path = str(tmp_path / "test.db")
        _connect(db_path).close()  # schema 初期化のみ（行は INSERT しない）

        state: ScanState = {
            "market_data": {"4519": _make_df([1000.0] * 30)},
            "votes": {"4519": {"p1": True, "p2": False, "p3": False, "p4": True,
                                "p5": False, "consensus": 1, "dev": -0.06}},
            "position_size": {}, "errors": [], "db_path": db_path, "broker": None,
            "book": "p1m", "book_equity": 1_000_000.0,
            "book_free_cash": 1_000_000.0, "book_flex": False,
            "book_regime_filter": False, "book_open_tickers": set(),
        }
        kelly_node(state)

        con = duckdb.connect(db_path, read_only=True)
        count = con.execute("SELECT COUNT(*) FROM decision_shadow").fetchone()[0]
        con.close()
        assert count == 0
