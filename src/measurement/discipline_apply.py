"""規律エンジン硬化: GO済仮説の監査可能・冪等・人間ゲート付き apply/revert 経路。

blueprint §9 / S2→S3 昇格ゲート③「適用/revert経路を監査可能・冪等に
instrument（≥1回の実適用+即revertを冪等に実行できた証拠）」の実装。
2026 SOTA（MAST 2503.13657 / reward-hacking均衡 2603.28063）が
「promptで代替不能・構造的に必須」と証明した転用kernel中核。

不可侵の構造（強い→弱いの順・正直に）:
- **GOゲート（一次防壁・偽装不能）**: apply は対象仮説の hypothesis_shadow
  最新 verdict=='GO' を要求。GO は決定論backtest WF が算出＝提案者が捏造不能。
- **適用Δはゲート記録値のみ**: 呼出側は任意paramをsmuggleできない。
  1変更ルール（Guardian）+ 既知StrategyParamsフィールドのみ。
- **単一active不変条件**: 異なる仮説が適用中なら apply 拒否（先に revert 必須）。
  silent な上書きを禁止。
- **generator≠applier 分離（運用規律）**: hypothesis_loop（生成・Shadow）は
  本モジュールを呼ばない別経路。loop自身に適用コード無し。
- **人間承認（防御の多層化）**: 明示 APPROVAL_TOKEN + 理由を要求。ただし
  TOKENは公開定数＝これ単体は一次防壁でない（一次はGOゲート+台帳）。
- **DEFAULTS(frozen)は不変**: effective_params は dataclasses.replace で
  新インスタンスを返すのみ（単一ソースを書き換えない）。
- **revert は安全方向**: 理由のみ要求（GO/承認不要）。常に baseline へ戻せる。
- 監査台帳は append-only。現在状態は台帳から決定論的に導出（状態の二重持ち無し）。
"""
from __future__ import annotations

import json
import logging
from dataclasses import fields, replace
from datetime import datetime
from typing import Any

import duckdb

from config.strategy_params import DEFAULTS, StrategyParams
from src.data.db import get_connection

logger = logging.getLogger("measurement.discipline_apply")

# 人間ゲート（防御多層化の一層・一次防壁はGOゲート）。自律経路は渡さない。
APPROVAL_TOKEN = "grove-approved"

_VALID_FIELDS = {f.name for f in fields(StrategyParams)}

_SEQ_DDL = "CREATE SEQUENCE IF NOT EXISTS param_override_audit_id_seq START 1"
_TBL_DDL = """
CREATE TABLE IF NOT EXISTS param_override_audit (
    id            INTEGER DEFAULT nextval('param_override_audit_id_seq') PRIMARY KEY,
    ts            TIMESTAMP NOT NULL,
    action        VARCHAR NOT NULL,          -- 'apply' | 'revert'
    hypothesis    VARCHAR,                   -- apply時の仮説名
    param_delta   TEXT NOT NULL,             -- 適用Δ(JSON, revert時は {})
    gate_eval_date DATE,                     -- GOを出した hypothesis_shadow.eval_date
    windows_passed INTEGER,
    approval_actor VARCHAR NOT NULL,         -- 承認主体（人間）
    reason        TEXT NOT NULL,
    prev_active   TEXT NOT NULL,             -- 直前のactive override(JSON)
    new_active    TEXT NOT NULL              -- 適用後のactive override(JSON)
)
"""


def _ensure(con) -> None:
    """監査台帳スキーマを冪等作成（各publicエントリで1回。helperは再実行しない）。"""
    con.execute(_SEQ_DDL)
    con.execute(_TBL_DDL)


def _active_state(con) -> tuple[dict[str, Any], str | None, str | None]:
    """台帳最新行から (active override, その仮説名, 最終action) を決定論導出。
    呼出側が _ensure 済である前提（helperはDDLを再実行しない）。"""
    row = con.execute(
        "SELECT new_active, hypothesis, action FROM param_override_audit "
        "ORDER BY id DESC LIMIT 1"
    ).fetchone()
    if not row:
        return {}, None, None
    return json.loads(row[0]), row[1], row[2]


def _active_override(con) -> dict[str, Any]:
    return _active_state(con)[0]


def _validate_delta(delta: dict) -> None:
    """Guardian 1変更ルール + 既知フィールドのみ（typo/injection遮断）。
    内部raise。publicは refused に正規化して返す（契約統一）。"""
    if len(delta) != 1:
        raise ValueError(f"1変更ルール違反: {delta}")
    key = next(iter(delta))
    if key not in _VALID_FIELDS:
        raise ValueError(f"未知のStrategyParamsフィールド: {key}")


def _latest_go(con, name: str):
    """対象仮説の最新 hypothesis_shadow 行。テーブル未作成のみ None。
    スキーマ破損(列欠落等)は隠さず再送出（no-row偽装＝自己欺瞞の防止）。"""
    try:
        return con.execute(
            "SELECT verdict, param_delta, eval_date, windows_passed "
            "FROM hypothesis_shadow WHERE name = ? "
            "ORDER BY eval_date DESC, id DESC LIMIT 1",
            [name],
        ).fetchone()
    except duckdb.CatalogException:
        return None  # hypothesis_shadow 未作成＝GO実績なし（正当な不在）


def apply_hypothesis(
    name: str, *, approval: str, reason: str, db_path: str | None = None
) -> dict:
    """GO済仮説を承認付きで適用（監査・冪等・単一active）。guard不成立は適用せず
    理由を返す（全guardは raise でなく refused に統一）。"""
    con = get_connection(db_path)
    try:
        _ensure(con)
        if approval != APPROVAL_TOKEN or not reason.strip():
            return {"status": "refused", "why": "human approval/reason required"}
        go = _latest_go(con, name)
        if go is None:
            return {"status": "refused", "why": f"no hypothesis_shadow row: {name}"}
        verdict, delta_json, eval_date, wpass = go
        if verdict != "GO":
            return {"status": "refused", "why": f"verdict={verdict} (need GO)"}
        delta = json.loads(delta_json)
        try:
            _validate_delta(delta)  # ゲート記録Δのみ・1変更・既知field
        except ValueError as e:
            return {"status": "refused", "why": str(e)}
        cur_ov, cur_hyp, _ = _active_state(con)
        if cur_ov:
            if cur_hyp == name and cur_ov == delta:
                return {"status": "noop", "active": cur_ov}  # 冪等(同一)
            return {"status": "refused",
                    "why": f"active override exists ({cur_hyp}); revert first"}
        con.execute(
            "INSERT INTO param_override_audit (ts,action,hypothesis,param_delta,"
            "gate_eval_date,windows_passed,approval_actor,reason,prev_active,"
            "new_active) VALUES (?,?,?,?,?,?,?,?,?,?)",
            [datetime.now(), "apply", name, json.dumps(delta), eval_date,
             wpass, "grove", reason.strip(), json.dumps(cur_ov),
             json.dumps(delta)],
        )
        con.execute(
            "UPDATE hypothesis_shadow SET applied = TRUE "
            "WHERE name = ? AND eval_date = ?", [name, eval_date]
        )
        logger.info("applied %s Δ=%s (gate %s, %s窓) reason=%s",
                    name, delta, eval_date, wpass, reason.strip())
        return {"status": "applied", "hypothesis": name, "delta": delta,
                "prev_active": cur_ov}
    finally:
        con.close()


def revert(*, reason: str, db_path: str | None = None) -> dict:
    """active override を baseline へ戻す（安全方向・理由のみ要求・冪等）。"""
    con = get_connection(db_path)
    try:
        _ensure(con)
        if not reason.strip():
            return {"status": "refused", "why": "reason required for audit"}
        current = _active_override(con)
        if not current:
            return {"status": "noop", "active": {}}  # 冪等: 既にbaseline
        con.execute(
            "INSERT INTO param_override_audit (ts,action,hypothesis,param_delta,"
            "gate_eval_date,windows_passed,approval_actor,reason,prev_active,"
            "new_active) VALUES (?,?,?,?,?,?,?,?,?,?)",
            [datetime.now(), "revert", None, json.dumps({}), None, None,
             "grove", reason.strip(), json.dumps(current), json.dumps({})],
        )
        con.execute("UPDATE hypothesis_shadow SET applied = FALSE "
                    "WHERE applied = TRUE")
        logger.info("reverted to baseline (was %s) reason=%s",
                    current, reason.strip())
        return {"status": "reverted", "prev_active": current}
    finally:
        con.close()


def effective_params(db_path: str | None = None) -> StrategyParams:
    """DEFAULTS に承認済みactive overrideを重ねた**新インスタンス**を返す。
    DEFAULTS(frozen単一ソース)は書き換えない。override無し=DEFAULTSと同値。

    注意: get_connection は毎回フルDDLを走らせる。**per-ticker等のホット
    パスから直接呼ばない**こと（配線時は scan 単位で1回呼び結果をキャッシュ）。
    """
    con = get_connection(db_path)
    try:
        _ensure(con)
        ov = _active_override(con)
    finally:
        con.close()
    return replace(DEFAULTS, **ov) if ov else DEFAULTS
