"""S7: ADVバッチ取得の再開可能性・チェックポイント・429耐性（モック・無network）。

Grove方針: 1ルートで全1,800投げず時差バッチでパンク防止。本テストは
「途中保存され再実行で続きから」「max_batchesで打ち切れる」「429は
保存を保ったまま停止し再開可」を固定する。
"""
from __future__ import annotations

import pandas as pd
import pytest

from src.data import universe as U


def _prime(n: int) -> pd.DataFrame:
    codes = [f"{1000 + i}0" for i in range(n)]
    return pd.DataFrame({"Code": codes, "CoName": codes, "S33": "0050",
                         "S33Nm": "x", "ScaleCat": "y", "ticker": codes})


class _FakeClient:
    """get_eq_bars_daily が Va 付き df を返す。fail_on の code で例外。"""
    def __init__(self, va=2e8, fail_on=None, exc=None):
        self.va, self.fail_on, self.exc = va, set(fail_on or []), exc
        self.calls = []

    def get_eq_bars_daily(self, code, from_yyyymmdd, to_yyyymmdd):
        self.calls.append(code)
        if code in self.fail_on:
            raise self.exc
        return pd.DataFrame({"Va": [self.va] * 25})


@pytest.fixture
def db(tmp_path):
    return str(tmp_path / "uni.duckdb")


def test_checkpoint_and_resume(db, monkeypatch):
    prime = _prime(10)
    fc = _FakeClient(va=2e8)
    monkeypatch.setattr(U, "_get_client", lambda: fc)
    # 1回目: max_batches=1, batch_size=4 → 4件だけ処理して未完
    p1 = U.fetch_adv_progress(prime, batch_size=4, cooldown_sec=0,
                              max_batches=1, throttle_sec=0, db_path=db)
    assert p1 == {"complete": False, "done": 4, "total": 10,
                  "remaining": 6, "batches_this_run": 1}
    assert fc.calls == prime["Code"].tolist()[:4]

    # 2回目: 続きから（done済4件はスキップ）。残6を batch4 → 2バッチで完了
    fc2 = _FakeClient(va=2e8)
    monkeypatch.setattr(U, "_get_client", lambda: fc2)
    p2 = U.fetch_adv_progress(prime, batch_size=4, cooldown_sec=0,
                              max_batches=0, throttle_sec=0, db_path=db)
    assert p2["complete"] is True and p2["done"] == 10 and p2["remaining"] == 0
    # 2回目は残り6銘柄だけ叩く（再取得しない＝チェックポイント有効）
    assert fc2.calls == prime["Code"].tolist()[4:]


def test_already_complete_is_noop(db, monkeypatch):
    prime = _prime(5)
    monkeypatch.setattr(U, "_get_client", lambda: _FakeClient())
    U.fetch_adv_progress(prime, batch_size=10, cooldown_sec=0,
                         throttle_sec=0, db_path=db)
    fc = _FakeClient()
    monkeypatch.setattr(U, "_get_client", lambda: fc)
    p = U.fetch_adv_progress(prime, batch_size=10, cooldown_sec=0,
                             throttle_sec=0, db_path=db)
    assert p["complete"] is True and p["batches_this_run"] == 0
    assert fc.calls == []  # 全done → API一切叩かない


def test_429_stops_but_preserves_progress(db, monkeypatch):
    prime = _prime(8)
    fifth = prime["Code"].tolist()[4]
    fc = _FakeClient(fail_on=[fifth], exc=RuntimeError("429 too many"))
    monkeypatch.setattr(U, "_get_client", lambda: fc)

    # _call_with_backoff を「sleepせず・429なら即RuntimeError化」に差替
    # （実バックオフは最大~4分sleep。テストでは挙動だけ検証）
    def fake_backoff(fn, *, what="", **k):
        try:
            return fn()
        except Exception as e:  # noqa: BLE001
            if U._is_rate_limit(e):
                raise RuntimeError(f"rate limit not cleared for {what}: {e}")
            raise
    monkeypatch.setattr(U, "_call_with_backoff", fake_backoff)

    with pytest.raises(RuntimeError, match="ADV停止"):
        U.fetch_adv_progress(prime, batch_size=10, cooldown_sec=0,
                             throttle_sec=0, db_path=db)
    # 5件目で停止 → 1-4件目は保存済（再開可能）
    con = U._adv_db(db, read_only=True)
    n = con.execute("SELECT count(*) FROM adv_progress").fetchone()[0]
    con.close()
    assert n == 4


def test_build_filtered_and_reset(db, monkeypatch):
    prime = _prime(6)
    # 3件は高ADV(>=1億)、3件は低ADV(<1億) → フィルタで3件残る
    class C:
        def __init__(s): s.calls = []
        def get_eq_bars_daily(s, code, **k):
            s.calls.append(code)
            hi = code in prime["Code"].tolist()[:3]
            return pd.DataFrame({"Va": [2e8 if hi else 1e6] * 25})
    monkeypatch.setattr(U, "_get_client", lambda: C())
    U.fetch_adv_progress(prime, batch_size=10, cooldown_sec=0,
                         throttle_sec=0, db_path=db)
    f = U.build_filtered_from_progress(prime, adv_min_jpy=1e8, db_path=db)
    assert len(f) == 3
    assert set(f["Code"]) == set(prime["Code"].tolist()[:3])
    assert (f["adv_20d"] >= 1e8).all()
    # reset で消える
    U.reset_adv_progress(db_path=db)
    con = U._adv_db(db, read_only=True)
    assert con.execute("SELECT count(*) FROM adv_progress").fetchone()[0] == 0
    con.close()
