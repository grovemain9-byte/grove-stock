"""S7再設計: ADVをキャッシュ済 daily_quotes から算出（API二重取得排除）の検証。

compute_adv_from_cache（API呼出ゼロ・1クエリ集計）と prime_codes_cached
（再開/完了判定）を決定論で固定。
"""
from __future__ import annotations

from datetime import date, timedelta

import duckdb
import pandas as pd
import pytest

from src.data import universe as U


def _seed_cache(db: str, rows: list[tuple]):
    """rows: (code, date, value) を daily_quotes に投入。"""
    con = duckdb.connect(db)
    con.execute("""
        CREATE TABLE IF NOT EXISTS daily_quotes (
            code VARCHAR NOT NULL, date DATE NOT NULL,
            open DOUBLE, high DOUBLE, low DOUBLE, close DOUBLE,
            volume DOUBLE, value DOUBLE, PRIMARY KEY (code, date))
    """)
    for code, d, val in rows:
        con.execute("INSERT INTO daily_quotes VALUES (?,?,?,?,?,?,?,?)",
                    [code, d, 1, 1, 1, 1, 1, val])
    con.close()


def _prime(codes):
    return pd.DataFrame({"Code": codes, "ticker": codes, "CoName": codes,
                         "S33": "0050", "S33Nm": "x", "ScaleCat": "y"})


def test_compute_adv_from_cache_filters_by_value(tmp_path):
    db = str(tmp_path / "c.duckdb")
    today = date.today()
    rows = []
    # AAA: 25日 value=2億 → ADV 2億 >= 1億 → 残る
    # BBB: 25日 value=5千万 → ADV 5千万 < 1億 → 落ちる
    # CCC: 5日のみ(lookback//2=10未満) → 無効扱いで落ちる
    for i in range(25):
        d = today - timedelta(days=i)
        rows.append(("AAA", d, 2e8))
        rows.append(("BBB", d, 5e7))
    for i in range(5):
        rows.append(("CCC", today - timedelta(days=i), 9e8))
    _seed_cache(db, rows)

    f = U.compute_adv_from_cache(_prime(["AAA", "BBB", "CCC"]),
                                 adv_min_jpy=1e8, lookback_days=20, db_path=db)
    assert list(f["Code"]) == ["AAA"]
    assert f.iloc[0]["adv_20d"] == pytest.approx(2e8)


def test_compute_adv_uses_only_recent_lookback(tmp_path):
    db = str(tmp_path / "c2.duckdb")
    today = date.today()
    rows = []
    # 直近20日 value=2億、それ以前 value=1万 → ADV(直近20)=2億 で残るべき
    for i in range(20):
        rows.append(("XXX", today - timedelta(days=i), 2e8))
    for i in range(20, 60):
        rows.append(("XXX", today - timedelta(days=i), 1e4))
    _seed_cache(db, rows)
    f = U.compute_adv_from_cache(_prime(["XXX"]), adv_min_jpy=1e8,
                                 lookback_days=20, db_path=db)
    assert list(f["Code"]) == ["XXX"]
    assert f.iloc[0]["adv_20d"] == pytest.approx(2e8)  # 古い1万に汚染されない


def test_prime_codes_cached_recency_and_minbars(tmp_path):
    db = str(tmp_path / "c3.duckdb")
    today = date.today()
    rows = []
    # GOOD: 70本・直近 → cached
    for i in range(70):
        rows.append(("GOOD", today - timedelta(days=i), 1e8))
    # FEW: 5本のみ → min_bars未満 → not cached
    for i in range(5):
        rows.append(("FEW", today - timedelta(days=i), 1e8))
    # STALE: 70本だが古い(最終200日前) → recency外 → not cached
    for i in range(70):
        rows.append(("STALE", today - timedelta(days=200 + i), 1e8))
    _seed_cache(db, rows)
    got = U.prime_codes_cached(["GOOD", "FEW", "STALE"],
                               recent_within_days=10, min_bars=60, db_path=db)
    assert got == {"GOOD"}


def test_empty_prime_safe(tmp_path):
    db = str(tmp_path / "c4.duckdb")
    _seed_cache(db, [("ZZZ", date.today(), 1e8)])
    assert U.prime_codes_cached([], db_path=db) == set()
    f = U.compute_adv_from_cache(_prime([]), db_path=db)
    assert len(f) == 0


def test_build_truncated_by_max_batches_skips_save(tmp_path, monkeypatch):
    """max_batches で pending を全消化できない → complete=False・save呼ばない。"""
    import scripts.build_universe as B

    prime = _prime(["A1", "A2", "A3", "A4", "A5"])
    monkeypatch.setattr(B, "fetch_universe", lambda **k: prime)
    monkeypatch.setattr(B, "prime_codes_cached", lambda codes, **k: set())  # 全未cached
    called = {"save": 0}
    monkeypatch.setattr(B, "bulk_fetch", lambda b, **k: {"ok": len(b)})
    monkeypatch.setattr(B, "save_universe",
                        lambda f: called.__setitem__("save", called["save"] + 1))
    # batch2 × max1 → pending5中2件だけ処理 → truncated → 未完了
    r = B.build(batch_size=2, cooldown_sec=0, max_batches=1)
    assert r["complete"] is False
    assert called["save"] == 0  # 打切→保存しない（494保護）


def test_build_completes_excludes_datapoor_and_min_sane(tmp_path, monkeypatch):
    """全pending試行済なら data-poor 数銘柄が60本未満でも完了し、
    compute_adv で自然除外。filtered<MIN_SANE は save せず RuntimeError。"""
    import scripts.build_universe as B

    prime = _prime([f"C{i}" for i in range(10)])
    monkeypatch.setattr(B, "fetch_universe", lambda **k: prime)
    monkeypatch.setattr(B, "prime_codes_cached", lambda codes, **k: set())
    monkeypatch.setattr(B, "bulk_fetch", lambda b, **k: {"ok": len(b)})
    called = {"save": 0}
    monkeypatch.setattr(B, "save_universe",
                        lambda f: called.__setitem__("save", called["save"] + 1))
    # filtered が MIN_SANE 未満（2銘柄）→ 保存せず RuntimeError（494保護の二重網）
    monkeypatch.setattr(B, "compute_adv_from_cache",
                        lambda prime, **k: prime.head(2).assign(adv_20d=9e9))
    with pytest.raises(RuntimeError, match="MIN_SANE|< 300|上書きせず"):
        B.build(batch_size=100, cooldown_sec=0, max_batches=0)  # max0=全pending消化
    assert called["save"] == 0
