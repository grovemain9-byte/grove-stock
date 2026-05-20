"""S5 計測インフラ検証: capital_tracker / decision_shadow / walk_forward。

最重要: decision_shadow が「決定日に勝敗を即日計上しない」(build_diary の
即時計上バグの制度的再発防止) を回帰テストで固定する。
"""
from __future__ import annotations

from datetime import date, timedelta

import numpy as np
import pandas as pd
import pytest

from src.data.db import get_connection
from src.measurement import capital_tracker, decision_shadow
from src.measurement.walk_forward import purged_splits, metrics_from_equity, compare


# === capital_tracker: ブック毎スナップショット・同日冪等 ===

class TestCapitalTracker:
    def _seed(self, db):
        con = get_connection(db)
        # p1m: closed +5000(realized), open committed=2000*100+220
        con.execute(
            "INSERT INTO positions (ticker,entry_price,shares,entry_date,status,"
            "exit_reason,pnl,commission,book) VALUES "
            "('C',1000,100,?, 'closed','take_profit',5000,200,'p1m')", [date(2026, 5, 1)])
        con.execute(
            "INSERT INTO positions (ticker,entry_price,shares,entry_date,status,"
            "commission,book) VALUES ('O',2000,100,?, 'open',220,'p1m')",
            [date(2026, 5, 12)])
        con.close()

    def test_snapshot_all_books_and_accounting(self, tmp_path):
        db = str(tmp_path / "cap.db")
        self._seed(db)
        rows = capital_tracker.snapshot(db, on=date(2026, 5, 16))
        assert {r["book"] for r in rows} == {"p1m", "p5m", "p10m", "p30m", "p50m"}
        p1m = next(r for r in rows if r["book"] == "p1m")
        assert p1m["equity"] == 1_005_000.0           # 初期+realized
        assert p1m["committed"] == pytest.approx(200_220.0)
        assert p1m["free_cash"] == pytest.approx(1_005_000.0 - 200_220.0)
        assert p1m["open_count"] == 1
        assert p1m["slots_left"] == 6                  # max7 - open1

    def test_idempotent_same_day(self, tmp_path):
        db = str(tmp_path / "cap2.db")
        self._seed(db)
        capital_tracker.snapshot(db, on=date(2026, 5, 16))
        capital_tracker.snapshot(db, on=date(2026, 5, 16))  # 2回目
        con = get_connection(db)
        n = con.execute(
            "SELECT count(*) FROM capital_state WHERE snapshot_date=?",
            [date(2026, 5, 16)]).fetchone()[0]
        con.close()
        assert n == 5  # 5ブック分のみ（重複しない）


# === decision_shadow: 即日計上禁止（最重要回帰） ===

class TestDecisionShadowNoImmediateAccounting:
    def _bars(self, start: date, closes: list[float]) -> pd.DataFrame:
        dates = pd.bdate_range(start, periods=len(closes))
        return pd.DataFrame({
            "date": dates, "open": closes, "high": [c + 1 for c in closes],
            "low": [c - 1 for c in closes], "close": closes,
            "volume": [1e6] * len(closes),
        })

    def test_record_then_cf_null_until_exit(self, tmp_path, monkeypatch):
        db = str(tmp_path / "ds.db")
        dec = date(2026, 3, 2)
        rid = decision_shadow.record_proposal(
            ticker="7203", decision="pass", decided_date=dec,
            proposed_yen=300000, consensus=3, edge=0.12,
            council_reason="DA: overfit risk", db_path=db)
        con = get_connection(db)
        row = con.execute(
            "SELECT decision,cf_pnl_pct,cf_computed_at FROM decision_shadow WHERE id=?",
            [rid]).fetchone()
        con.close()
        assert row[0] == "pass"
        assert row[1] is None and row[2] is None  # 記録直後: 反実仮想は未確定

        # 出口未到達 → backfill しても NULL のまま（即日計上しない）。
        # 条件: バー<25 で MA25=NaN→take_profit不発、緩降下で -7%未満→stop不発、
        # <15営業日→max_hold不発。＝still_open。
        mild = [1000, 998, 996, 994, 992, 990, 988, 986, 984, 982, 980, 978]
        monkeypatch.setattr(decision_shadow, "load_bars",
                            lambda t: self._bars(dec, mild))
        filled = decision_shadow.backfill_counterfactuals(db_path=db)
        assert filled == 0
        con = get_connection(db)
        cf = con.execute(
            "SELECT cf_pnl_pct,cf_won,cf_computed_at FROM decision_shadow WHERE id=?",
            [rid]).fetchone()
        con.close()
        assert cf == (None, None, None)  # 横ばいで決済つかない=NULL維持

    def test_cf_uses_exit_day_not_decision_day(self, tmp_path, monkeypatch):
        db = str(tmp_path / "ds2.db")
        dec = date(2026, 3, 2)
        rid = decision_shadow.record_proposal(
            ticker="9999", decision="go", decided_date=dec,
            consensus=4, db_path=db)
        # 入口1000、数日横ばい後に -8%(>STOP_LOSS -7%) を *後の営業日* で踏む
        closes = [1000, 1000, 1000, 1000, 1000, 1000, 920] + [900] * 30
        monkeypatch.setattr(decision_shadow, "load_bars",
                            lambda t: self._bars(dec, closes))
        filled = decision_shadow.backfill_counterfactuals(db_path=db)
        assert filled == 1
        con = get_connection(db)
        r = con.execute(
            "SELECT cf_exit_date,cf_exit_reason,cf_pnl_pct,cf_won,cf_computed_at "
            "FROM decision_shadow WHERE id=?", [rid]).fetchone()
        con.close()
        cf_exit, reason, pnl, won, computed = r
        assert reason == "stop_loss"
        assert cf_exit > dec, "出口日が決定日より後（即日計上でない）"
        assert pnl < 0 and won is False and computed is not None

    def test_backfill_idempotent(self, tmp_path, monkeypatch):
        db = str(tmp_path / "ds3.db")
        dec = date(2026, 3, 2)
        decision_shadow.record_proposal(
            ticker="9999", decision="go", decided_date=dec, db_path=db)
        closes = [1000] * 5 + [900] * 30
        monkeypatch.setattr(decision_shadow, "load_bars",
                            lambda t: self._bars(dec, closes))
        assert decision_shadow.backfill_counterfactuals(db_path=db) == 1
        # 2回目: 既に確定済→0件、行は不変
        con = get_connection(db)
        before = con.execute("SELECT cf_pnl_pct FROM decision_shadow").fetchone()[0]
        con.close()
        assert decision_shadow.backfill_counterfactuals(db_path=db) == 0
        con = get_connection(db)
        after = con.execute("SELECT cf_pnl_pct FROM decision_shadow").fetchone()[0]
        con.close()
        assert before == after

    def test_record_proposal_is_idempotent_on_same_key(self, tmp_path):
        """UNIQUE(ticker, book, decided_date): 同日同 (ticker, book) で再 record されたら
        最新の判断で UPDATE される（INSERT されない）。

        2026-05-20: cron 9時/12時/15時 + 手動 paper run で同日複数回 record_proposal が
        呼ばれる → 重複記録を防ぐため UNIQUE 制約 + ON CONFLICT DO UPDATE を導入。
        """
        db = str(tmp_path / "ds_idemp.db")
        dec = date(2026, 5, 20)

        # 1回目: pass (regime_filter_skip)
        rid1 = decision_shadow.record_proposal(
            ticker="4519", decision="pass", decided_date=dec, book="p5m",
            consensus=4, edge=-0.06, council_reason="regime_filter_skip:p4=False",
            db_path=db)

        # 2回目: 同じ (ticker, book, decided_date) で別の判断 (go) → UPDATE
        rid2 = decision_shadow.record_proposal(
            ticker="4519", decision="go", decided_date=dec, book="p5m",
            consensus=4, edge=-0.07, proposed_yen=100000,
            council_reason="kelly_ok", db_path=db)

        con = get_connection(db)
        rows = con.execute(
            "SELECT id, decision, edge, council_reason, proposed_yen "
            "FROM decision_shadow WHERE ticker='4519' AND book='p5m' AND decided_date=?",
            [dec]).fetchall()
        con.close()
        # UNIQUE 制約により 1 行のみ
        assert len(rows) == 1
        # 同じ id（ON CONFLICT DO UPDATE は新 INSERT しない）
        assert rid1 == rid2
        # 最新の値が残っている
        assert rows[0][1] == "go"
        assert rows[0][2] == pytest.approx(-0.07)
        assert rows[0][3] == "kelly_ok"
        assert rows[0][4] == 100000

    def test_record_proposal_different_book_separately_stored(self, tmp_path):
        """同 ticker × decided_date でも book が異なれば別行として記録される。"""
        db = str(tmp_path / "ds_book.db")
        dec = date(2026, 5, 20)
        decision_shadow.record_proposal(
            ticker="4519", decision="go", decided_date=dec, book="p1m",
            consensus=4, db_path=db)
        decision_shadow.record_proposal(
            ticker="4519", decision="pass", decided_date=dec, book="p5m",
            consensus=4, council_reason="regime_filter_skip:p4=False", db_path=db)

        con = get_connection(db)
        n = con.execute(
            "SELECT count(*) FROM decision_shadow "
            "WHERE ticker='4519' AND decided_date=?", [dec]).fetchone()[0]
        con.close()
        assert n == 2


# === walk_forward: purge/embargo 不変条件 ===

class TestWalkForward:
    def test_purge_embargo_gap_respected(self):
        embargo = 15
        folds = list(purged_splits(500, n_splits=5, embargo=embargo))
        assert len(folds) >= 1
        for train, test in folds:
            # train末尾と test先頭の間に >= embargo の空白（リーク防止）
            assert min(test) - max(train) > embargo
            # disjoint & 時系列順
            assert set(train).isdisjoint(set(test))
            assert max(train) < min(test)

    def test_no_folds_when_too_small(self):
        assert list(purged_splits(3, n_splits=5)) == []

    def test_metrics_engine_vs_empyrical_agree(self):
        eq = pd.Series(1000 * np.cumprod(1 + np.random.default_rng(1).normal(0, 0.02, 300)))
        m = metrics_from_equity(eq)
        assert m["max_drawdown_empyrical"] == pytest.approx(
            m["max_drawdown_engine"], abs=1e-12)

    def test_compare_returns_deltas(self):
        rng = np.random.default_rng(7)
        dyn = pd.Series(1000 * np.cumprod(1 + rng.normal(0.0005, 0.02, 200)))
        base = pd.Series(1000 * np.cumprod(1 + rng.normal(0.0, 0.02, 200)))
        c = compare(dyn, base)
        assert "sharpe" in c and "delta" in c["sharpe"]
        assert c["sharpe"]["delta"] == pytest.approx(
            c["sharpe"]["dynamic"] - c["sharpe"]["baseline"], abs=1e-12)
