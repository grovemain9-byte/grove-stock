"""discipline_apply: 監査可能・冪等・人間ゲート付き apply/revert の敵対的検証。

blueprint S2→S3 昇格ゲート③「≥1回の実適用+即revertを冪等に実行できた証拠」。
backtest非依存・微小tmp duckdbのみ＝非integration・高速。
スキーマは hypothesis_loop._DDL を単一ソース流用（手書きコピーのdrift防止）。
"""
from __future__ import annotations

import json
from datetime import date

import pytest

from config.strategy_params import DEFAULTS
from src.data.db import get_connection
from src.measurement import discipline_apply as da
from src.measurement.hypothesis_loop import _DDL as SHADOW_DDL


def _seed_shadow(db, name, delta, verdict, eval_date="2026-05-19"):
    con = get_connection(db)
    for stmt in SHADOW_DDL.strip().split(";"):  # 単一ソースのschemaを流用
        if stmt.strip():
            con.execute(stmt)
    con.execute(
        "INSERT INTO hypothesis_shadow "
        "(eval_date,name,param_delta,windows_json,windows_passed,verdict,applied)"
        " VALUES (?,?,?,?,?,?,FALSE)",
        [date.fromisoformat(eval_date), name, json.dumps(delta), "[]",
         3 if verdict == "GO" else 1, verdict],
    )
    con.close()


def _applied_flag(db, name):
    con = get_connection(db)
    r = con.execute("SELECT applied FROM hypothesis_shadow WHERE name=?",
                    [name]).fetchone()
    con.close()
    return r[0]


@pytest.fixture
def db(tmp_path):
    return str(tmp_path / "t.duckdb")


# --- guard（一次防壁=GOゲート / 全guardは refused に統一・raise しない）---

def test_refuse_no_shadow_row(db):
    get_connection(db).close()
    r = da.apply_hypothesis("regime_filter", approval=da.APPROVAL_TOKEN,
                            reason="x", db_path=db)
    assert r["status"] == "refused"


def test_refuse_not_go(db):
    _seed_shadow(db, "stop_tight_5", {"stop_loss": -0.05}, "PASS")
    r = da.apply_hypothesis("stop_tight_5", approval=da.APPROVAL_TOKEN,
                            reason="x", db_path=db)
    assert r["status"] == "refused" and "GO" in r["why"]


def test_refuse_bad_approval(db):
    _seed_shadow(db, "regime_filter", {"p4_required": True}, "GO")
    assert da.apply_hypothesis("regime_filter", approval="nope",
                               reason="x", db_path=db)["status"] == "refused"
    assert da.apply_hypothesis("regime_filter", approval=da.APPROVAL_TOKEN,
                               reason="  ", db_path=db)["status"] == "refused"


def test_multi_or_unknown_param_refused_not_raised(db):
    """H1契約: 不正Δは raise でなく refused（caller契約統一）。"""
    _seed_shadow(db, "bad_multi", {"stop_loss": -0.05, "consensus_min": 4}, "GO")
    r1 = da.apply_hypothesis("bad_multi", approval=da.APPROVAL_TOKEN,
                             reason="x", db_path=db)
    assert r1["status"] == "refused" and "1変更" in r1["why"]
    _seed_shadow(db, "bad_field", {"not_a_field": 1}, "GO")
    r2 = da.apply_hypothesis("bad_field", approval=da.APPROVAL_TOKEN,
                             reason="x", db_path=db)
    assert r2["status"] == "refused" and "未知" in r2["why"]


# --- apply / revert / 冪等 / 非破壊 ---

def test_apply_then_revert_roundtrip_idempotent(db):
    _seed_shadow(db, "regime_filter", {"p4_required": True}, "GO")
    base = DEFAULTS

    r1 = da.apply_hypothesis("regime_filter", approval=da.APPROVAL_TOKEN,
                             reason="S1 GO confirmed", db_path=db)
    assert r1["status"] == "applied"
    eff = da.effective_params(db)
    assert eff.p4_required is True            # overrideが効く
    assert DEFAULTS.p4_required is False      # frozen単一ソースは不変
    assert eff is not DEFAULTS                # 新インスタンス
    assert _applied_flag(db, "regime_filter") is True

    # 冪等: 同一仮説・同一Δ再適用 = noop
    assert da.apply_hypothesis("regime_filter", approval=da.APPROVAL_TOKEN,
                               reason="again", db_path=db)["status"] == "noop"

    # revert: baseline完全復元
    rv = da.revert(reason="rollback test", db_path=db)
    assert rv["status"] == "reverted"
    eff2 = da.effective_params(db)
    assert eff2.p4_required == base.p4_required
    assert eff2 == DEFAULTS
    assert _applied_flag(db, "regime_filter") is False

    # 冪等: revert再実行 = noop
    assert da.revert(reason="again", db_path=db)["status"] == "noop"


def test_single_active_invariant_blocks_silent_switch(db):
    """M2: 別仮説が適用中なら apply 拒否（先に revert 必須・silent上書き禁止）。"""
    _seed_shadow(db, "regime_filter", {"p4_required": True}, "GO")
    _seed_shadow(db, "stop_tight_5", {"stop_loss": -0.05}, "GO")

    assert da.apply_hypothesis("regime_filter", approval=da.APPROVAL_TOKEN,
                               reason="A", db_path=db)["status"] == "applied"
    # 別仮説B: revert前は拒否
    rB = da.apply_hypothesis("stop_tight_5", approval=da.APPROVAL_TOKEN,
                             reason="B", db_path=db)
    assert rB["status"] == "refused" and "revert first" in rB["why"]
    # Aは効いたまま（B不可侵）
    assert da.effective_params(db).p4_required is True
    assert da.effective_params(db).stop_loss == DEFAULTS.stop_loss
    # revert後はBを適用可
    da.revert(reason="switch to B", db_path=db)
    assert da.apply_hypothesis("stop_tight_5", approval=da.APPROVAL_TOKEN,
                               reason="B", db_path=db)["status"] == "applied"
    assert da.effective_params(db).stop_loss == -0.05


def test_revert_noop_on_empty_ledger(db):
    get_connection(db).close()
    assert da.revert(reason="nothing to do", db_path=db)["status"] == "noop"
    assert da.effective_params(db) == DEFAULTS


def test_effective_params_is_defaults_when_clean(db):
    get_connection(db).close()
    assert da.effective_params(db) == DEFAULTS


def test_audit_trail_append_only(db):
    _seed_shadow(db, "regime_filter", {"p4_required": True}, "GO")
    da.apply_hypothesis("regime_filter", approval=da.APPROVAL_TOKEN,
                        reason="apply", db_path=db)
    da.revert(reason="revert", db_path=db)
    con = get_connection(db)
    rows = con.execute(
        "SELECT action,reason,prev_active,new_active FROM param_override_audit "
        "ORDER BY id").fetchall()
    con.close()
    assert [r[0] for r in rows] == ["apply", "revert"]
    assert rows[0][2] == "{}" and json.loads(rows[0][3])     # apply: {}→Δ
    assert json.loads(rows[1][2]) and rows[1][3] == "{}"     # revert: Δ→{}
