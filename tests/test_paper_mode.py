"""① 実価格ペーパーモード と ② DuckDBロックリトライ の検証。

なぜ: liveは立花ログイン失敗で978シグナル→3取引しか記録できず、DuckDBロック
競合でscan/daily_updateが落ちていた。本テストは修正の核を固定する。
"""
from __future__ import annotations

from unittest.mock import patch

import duckdb
import pytest

from src.broker.tachibana import MockTachibanaClient
from src.data.db import connect_duckdb, get_connection
from src.main import execution_node
from tests.test_scan_graph import _make_df


# === ② connect_duckdb: ロックリトライ ===

def test_connect_duckdb_success(tmp_path):
    con = connect_duckdb(tmp_path / "ok.duckdb")
    con.execute("CREATE TABLE t(x INTEGER)")
    con.close()


def test_connect_duckdb_retries_then_succeeds(tmp_path):
    real = duckdb.connect(str(tmp_path / "r.duckdb"))
    calls = {"n": 0}

    def flaky(*a, **k):
        calls["n"] += 1
        if calls["n"] < 3:
            raise duckdb.IOException("Could not set lock on file")
        return real

    with patch("src.data.db.duckdb.connect", side_effect=flaky):
        con = connect_duckdb(tmp_path / "r.duckdb", base_delay=0.001)
    assert con is real
    assert calls["n"] == 3


def test_connect_duckdb_non_lock_error_raises_immediately(tmp_path):
    def boom(*a, **k):
        raise duckdb.IOException("disk full, no space left")

    with patch("src.data.db.duckdb.connect", side_effect=boom):
        with pytest.raises(duckdb.IOException, match="disk full"):
            connect_duckdb(tmp_path / "x.duckdb", base_delay=0.001)


def test_connect_duckdb_gives_up_after_retries(tmp_path):
    def always_locked(*a, **k):
        raise duckdb.IOException("Could not set lock on file")

    with patch("src.data.db.duckdb.connect", side_effect=always_locked):
        with pytest.raises(RuntimeError, match="lock not released"):
            connect_duckdb(tmp_path / "y.duckdb", retries=3, base_delay=0.001)


# === ① 実価格ペーパー: Mockがダミー1000でなく実足終値で約定 ===

def test_paper_enters_at_real_close_not_dummy(tmp_path):
    """execution_node: Mock未価格設定でも market_data の実終値で約定する。

    未修正ならMock既定¥1000で記録される。修正後は2500近辺になる。
    """
    db_path = str(tmp_path / "paper.db")
    broker = MockTachibanaClient(initial_balance=1_000_000.0)
    state = {
        "position_size": {"9999": 100},
        "broker": broker,
        "market_data": {"9999": _make_df([2500.0] * 30)},
        "db_path": db_path,
        "errors": [],
    }

    execution_node(state)

    con = get_connection(db_path)
    rows = con.execute(
        "SELECT entry_price FROM positions WHERE status='open'"
    ).fetchall()
    con.close()
    assert rows, "ペーパー取引が記録されていない"
    price = rows[0][0]
    # Mockは±0.5%の約定ゆらぎ。2500近辺＝実終値約定（1000ダミーでない）。
    assert 2475.0 <= price <= 2525.0, f"実価格約定でない: {price}"
