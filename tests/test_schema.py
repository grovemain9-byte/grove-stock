"""Issue #1: DuckDBスキーマのテスト。

受け入れ基準:
- 各テーブルへのinsert・selectが正常動作すること
- status・exit_reasonに想定外の値が入った場合にエラーになること
"""
import pytest
import duckdb
from datetime import date, datetime
from pathlib import Path
from src.data.db import get_connection


@pytest.fixture
def con(tmp_path):
    """テスト用の一時DBを返す。"""
    db_path = tmp_path / "test.duckdb"
    c = get_connection(db_path)
    yield c
    c.close()


# --- scans テーブル ---

class TestScans:
    def test_insert_and_select(self, con):
        con.execute("""
            INSERT INTO scans (ticker, scanned_at, ma25_deviation, nikkei_change,
                               p1, p2, p3, p4, p5, consensus)
            VALUES ('4519', '2026-04-07 09:00:00', -13.3, -0.5,
                    TRUE, TRUE, TRUE, FALSE, FALSE, 3)
        """)
        row = con.execute("SELECT * FROM scans WHERE ticker = '4519'").fetchone()
        assert row is not None
        assert row[1] == "4519"  # ticker
        assert row[3] == pytest.approx(-13.3, abs=0.1)  # ma25_deviation
        assert row[10] == 3  # consensus

    def test_insert_multiple(self, con):
        for ticker in ("4502", "4507", "6758"):
            con.execute("""
                INSERT INTO scans (ticker, scanned_at, ma25_deviation, nikkei_change,
                                   p1, p2, p3, p4, p5, consensus)
                VALUES (?, '2026-04-07 09:30:00', -5.0, 0.1,
                        TRUE, FALSE, FALSE, TRUE, FALSE, 2)
            """, [ticker])
        count = con.execute("SELECT COUNT(*) FROM scans").fetchone()[0]
        assert count == 3

    def test_auto_increment_id(self, con):
        for i in range(3):
            con.execute("""
                INSERT INTO scans (ticker, scanned_at, ma25_deviation, nikkei_change,
                                   p1, p2, p3, p4, p5, consensus)
                VALUES ('TEST', '2026-04-07 09:00:00', -1.0, 0.0,
                        FALSE, FALSE, FALSE, FALSE, FALSE, 0)
            """)
        ids = [r[0] for r in con.execute("SELECT id FROM scans ORDER BY id").fetchall()]
        assert ids == [1, 2, 3]


# --- positions テーブル ---

class TestPositions:
    def test_insert_open_position(self, con):
        con.execute("""
            INSERT INTO positions (ticker, entry_price, shares, entry_date, status)
            VALUES ('4519', 8224.0, 100, '2026-04-07', 'open')
        """)
        row = con.execute("SELECT * FROM positions WHERE ticker = '4519'").fetchone()
        assert row is not None
        assert row[2] == pytest.approx(8224.0)  # entry_price
        assert row[3] == 100  # shares
        assert row[5] == "open"  # status
        assert row[6] is None  # exit_price
        assert row[7] is None  # exit_reason

    def test_close_position(self, con):
        con.execute("""
            INSERT INTO positions (ticker, entry_price, shares, entry_date, status)
            VALUES ('4519', 8224.0, 100, '2026-04-07', 'open')
        """)
        con.execute("""
            UPDATE positions
            SET status = 'closed',
                exit_price = 8800.0,
                exit_reason = 'take_profit',
                pnl = 57600.0,
                closed_at = '2026-04-10 14:30:00'
            WHERE ticker = '4519' AND status = 'open'
        """)
        row = con.execute("SELECT * FROM positions WHERE ticker = '4519'").fetchone()
        assert row[5] == "closed"
        assert row[6] == pytest.approx(8800.0)
        assert row[7] == "take_profit"
        assert row[8] == pytest.approx(57600.0)

    def test_invalid_status_rejected(self, con):
        with pytest.raises(duckdb.ConstraintException):
            con.execute("""
                INSERT INTO positions (ticker, entry_price, shares, entry_date, status)
                VALUES ('4519', 8224.0, 100, '2026-04-07', 'invalid_status')
            """)

    def test_invalid_exit_reason_rejected(self, con):
        with pytest.raises(duckdb.ConstraintException):
            con.execute("""
                INSERT INTO positions (ticker, entry_price, shares, entry_date, status,
                                       exit_reason)
                VALUES ('4519', 8224.0, 100, '2026-04-07', 'closed', 'unknown_reason')
            """)

    def test_all_valid_exit_reasons(self, con):
        reasons = ("stop_loss", "max_hold", "take_profit", "signal_reversal")
        for i, reason in enumerate(reasons):
            con.execute("""
                INSERT INTO positions (ticker, entry_price, shares, entry_date, status,
                                       exit_price, exit_reason, pnl, closed_at)
                VALUES (?, 1000.0, 100, '2026-04-07', 'closed',
                        1050.0, ?, 5000.0, '2026-04-10 15:00:00')
            """, [f"TEST{i}", reason])
        count = con.execute("SELECT COUNT(*) FROM positions WHERE status = 'closed'").fetchone()[0]
        assert count == 4

    def test_null_exit_reason_allowed_for_open(self, con):
        con.execute("""
            INSERT INTO positions (ticker, entry_price, shares, entry_date, status)
            VALUES ('4502', 5000.0, 200, '2026-04-07', 'open')
        """)
        row = con.execute("SELECT exit_reason FROM positions WHERE ticker = '4502'").fetchone()
        assert row[0] is None


# --- monthly_pnl テーブル ---

class TestMonthlyPnl:
    def test_insert_and_select(self, con):
        con.execute("""
            INSERT INTO monthly_pnl (year_month, total_pnl, drawdown_pct, is_stopped)
            VALUES ('2026-04', 50000.0, -3.2, FALSE)
        """)
        row = con.execute("SELECT * FROM monthly_pnl WHERE year_month = '2026-04'").fetchone()
        assert row[0] == "2026-04"
        assert row[1] == pytest.approx(50000.0)
        assert row[2] == pytest.approx(-3.2)
        assert row[3] is False

    def test_drawdown_stopper(self, con):
        con.execute("""
            INSERT INTO monthly_pnl (year_month, total_pnl, drawdown_pct, is_stopped)
            VALUES ('2026-04', -120000.0, -12.0, TRUE)
        """)
        row = con.execute("SELECT is_stopped FROM monthly_pnl WHERE year_month = '2026-04'").fetchone()
        assert row[0] is True

    def test_unique_year_month(self, con):
        con.execute("""
            INSERT INTO monthly_pnl (year_month, total_pnl, drawdown_pct, is_stopped)
            VALUES ('2026-04', 0.0, 0.0, FALSE)
        """)
        with pytest.raises(duckdb.ConstraintException):
            con.execute("""
                INSERT INTO monthly_pnl (year_month, total_pnl, drawdown_pct, is_stopped)
                VALUES ('2026-04', 100.0, -1.0, FALSE)
            """)

    def test_update_pnl(self, con):
        con.execute("""
            INSERT INTO monthly_pnl (year_month, total_pnl, drawdown_pct, is_stopped)
            VALUES ('2026-04', 0.0, 0.0, FALSE)
        """)
        con.execute("""
            UPDATE monthly_pnl
            SET total_pnl = total_pnl + 15000.0,
                drawdown_pct = -1.5
            WHERE year_month = '2026-04'
        """)
        row = con.execute("SELECT total_pnl FROM monthly_pnl WHERE year_month = '2026-04'").fetchone()
        assert row[0] == pytest.approx(15000.0)
