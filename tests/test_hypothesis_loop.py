"""継続ループ Shadow の門ロジック検証（mock・無backtest）。

検証: 3/3窓→GO / <3→PASS / 1変更ルール強制(Guardian) / 記録 applied=False
(=shadow 適用しない) / 同日冪等。
"""
from __future__ import annotations

from datetime import date

import pytest

from src.measurement import hypothesis_loop as H
from src.data.db import get_connection


@pytest.fixture(autouse=True)
def _no_sectors(monkeypatch):
    monkeypatch.setattr(H, "compute_sector_thresholds_from_cache",
                        lambda k=2.0: {"x": -0.05})


def _mock_engine(monkeypatch, table):
    """table[(p4_required, win_tag)] = metrics dict を返す run_backtest mock。
    G3(p4_required=False) と 候補(True) を窓別に作り分ける。"""
    class _R:
        def __init__(s, m): s._m = m
        def metrics(s): return s._m

    def fake(**kw):
        # 窓は end_date で識別（WINDOWS の end を使用）
        end = kw.get("end_date")
        tag = {"2024-04-30": "Y1", "2025-04-30": "Y2",
               "2026-05-15": "Y3"}.get(end, "?")
        key = (kw["p4_required"], tag)
        return _R(table.get(key, {"closed": 0}))
    monkeypatch.setattr(H, "run_backtest", fake)


def _m(closed, sharpe, mdd):
    return {"closed": closed, "sharpe_per_trade": sharpe,
            "max_drawdown": mdd, "avg_return": 0.01, "win_rate": 0.55}


def test_3of3_is_GO(monkeypatch):
    # 候補(True)が全窓で Sharpe↑ & maxDD非悪化 → GO
    tbl = {}
    for tag in ("Y1", "Y2", "Y3"):
        tbl[(False, tag)] = _m(100, 0.05, -0.30)   # G3
        tbl[(True, tag)] = _m(60, 0.15, -0.18)     # 候補=明確改善
    _mock_engine(monkeypatch, tbl)
    r = H.evaluate({"x": -0.05}, "regime_filter", {"p4_required": True}, {})
    assert r["windows_passed"] == 3 and r["verdict"] == "GO"


def test_2of3_is_PASS(monkeypatch):
    tbl = {}
    for tag in ("Y1", "Y2", "Y3"):
        tbl[(False, tag)] = _m(100, 0.05, -0.20)
    tbl[(True, "Y1")] = _m(60, 0.15, -0.10)   # pass
    tbl[(True, "Y2")] = _m(60, 0.15, -0.10)   # pass
    tbl[(True, "Y3")] = _m(60, 0.02, -0.30)   # fail (Sharpe↓ & DD悪化)
    _mock_engine(monkeypatch, tbl)
    r = H.evaluate({"x": -0.05}, "h", {"p4_required": True}, {})
    assert r["windows_passed"] == 2 and r["verdict"] == "PASS"


def test_guardian_blocks_multi_param():
    with pytest.raises(ValueError, match="1変更ルール違反"):
        H.evaluate({"x": -0.05}, "bad",
                   {"p4_required": True, "stop_loss": -0.05}, {})


def test_run_loop_records_shadow_not_applied(tmp_path, monkeypatch):
    db = str(tmp_path / "h.db")
    tbl = {}
    for tag in ("Y1", "Y2", "Y3"):
        tbl[(False, tag)] = _m(100, 0.05, -0.30)
        tbl[(True, tag)] = _m(60, 0.15, -0.18)
    _mock_engine(monkeypatch, tbl)
    # レジストリを1件に縮小（テスト高速化）
    monkeypatch.setattr(H, "HYPOTHESES", {"regime_filter": {"p4_required": True}})
    res = H.run_loop(db_path=db, on=date(2026, 5, 17))
    assert res[0]["verdict"] == "GO"
    con = get_connection(db)
    rows = con.execute(
        "SELECT name,verdict,applied,windows_passed FROM hypothesis_shadow"
    ).fetchall()
    con.close()
    assert rows == [("regime_filter", "GO", False, 3)]  # applied=False=shadow

    # 同日再実行で冪等（重複しない）
    H.run_loop(db_path=db, on=date(2026, 5, 17))
    con = get_connection(db)
    n = con.execute("SELECT count(*) FROM hypothesis_shadow").fetchone()[0]
    con.close()
    assert n == 1


def test_no_data_window_fails_safe(monkeypatch):
    tbl = {(False, "Y1"): _m(100, 0.05, -0.20),
           (True, "Y1"): _m(50, 0.15, -0.10)}
    # Y2/Y3 は table 無 → closed=0 → no_data → pass=False
    _mock_engine(monkeypatch, tbl)
    r = H.evaluate({"x": -0.05}, "h", {"p4_required": True}, {})
    assert r["windows_passed"] == 1 and r["verdict"] == "PASS"
