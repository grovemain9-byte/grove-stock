"""decision_shadow (plan S5 §2.2): go/pass 両方 + 事後反実仮想を記録。

agent_architecture_plan §4 / Grove要請: 却下(pass)からも学習する。全 go/pass に
proposal(¥X, edge, council理由) を記録し、**後日** 出口条件が実バーで成立して
から反実仮想 (cf_*) を埋める。

最重要の正しさ規約（build_diary 即時計上バグの制度的再発防止）:
    反実仮想は決定日に確定させない。shadow_replay._simulate_one を流用し
    (= engine.py の STOP_LOSS/MAX_HOLD/take_profit 出口ルール)、出口が
    実バーで成立した営業日基準でのみ cf_* を埋める。未到達(still_open)は
    cf_* を NULL のまま残す（勝敗を即日確定しない）。

冪等: backfill は cf_computed_at IS NULL の行のみ対象。再実行で二重計上しない。
"""
from __future__ import annotations

import logging
from datetime import date, datetime

import duckdb
import pandas as pd

from src.backtest.shadow_replay import _simulate_one
from src.backtest.engine import _compute_indicators
from src.data.cache import load_bars
from src.data.db import DEFAULT_DB_PATH

logger = logging.getLogger("measurement.decision_shadow")

_DDL = """
CREATE SEQUENCE IF NOT EXISTS decision_shadow_id_seq START 1;
CREATE TABLE IF NOT EXISTS decision_shadow (
    id INTEGER DEFAULT nextval('decision_shadow_id_seq') PRIMARY KEY,
    decided_at    TIMESTAMP NOT NULL,
    decided_date  DATE NOT NULL,
    ticker        VARCHAR NOT NULL,
    book          VARCHAR,
    decision      VARCHAR NOT NULL CHECK (decision IN ('go','pass')),
    proposed_yen  DOUBLE,
    consensus     INTEGER,
    edge          DOUBLE,
    council_reason TEXT,
    cf_exit_date  DATE,
    cf_exit_reason VARCHAR,
    cf_pnl_pct    DOUBLE,
    cf_won        BOOLEAN,
    cf_computed_at TIMESTAMP,
    -- v2 (Phase 0+ EVS/PLT 2026-05-21): 10 features + router provenance
    evs_total       DOUBLE,
    cell_id         VARCHAR,
    cap_pct_recommended DOUBLE,
    cap_pct_actual  DOUBLE,
    sizing_source   VARCHAR,
    cell_n_samples  INTEGER,
    cell_confidence VARCHAR,
    exploration_flag BOOLEAN,
    -- 10 feature values (JSON for forward-compat, also indexed cols for SQL)
    evs_components_json TEXT,
    UNIQUE (ticker, book, decided_date)
)
"""


def _connect(db_path: str | None) -> duckdb.DuckDBPyConnection:
    con = duckdb.connect(str(db_path) if db_path else str(DEFAULT_DB_PATH))
    for stmt in _DDL.strip().split(";"):
        if stmt.strip():
            con.execute(stmt)
    return con


def record_proposal(
    *,
    ticker: str,
    decision: str,
    decided_date: date,
    book: str | None = None,
    proposed_yen: float | None = None,
    consensus: int | None = None,
    edge: float | None = None,
    council_reason: str = "",
    db_path: str | None = None,
    # v2 (Phase 0+ EVS/PLT) — all optional for backward compat
    evs_total: float | None = None,
    cell_id: str | None = None,
    cap_pct_recommended: float | None = None,
    cap_pct_actual: float | None = None,
    sizing_source: str | None = None,
    cell_n_samples: int | None = None,
    cell_confidence: str | None = None,
    exploration_flag: bool | None = None,
    evs_components_json: str | None = None,
) -> int:
    """council の go/pass 提案を1行記録（cf_* は未確定=NULL）。

    冪等: UNIQUE(ticker, book, decided_date) で同日同 (ticker, book) の判断は
    ON CONFLICT DO UPDATE で「最新の判断」が残る。同日内で position 状態が変わって
    判断が遷移するケース (例: 朝 kelly_ok → 12時 already_open) を正しく反映する。
    cf_* は backfill_counterfactuals が別途埋めるため UPDATE 対象外。

    v2 (2026-05-21): EVS/PLT サイジングメタデータも保存。後付け学習・
    exploration evaluation のため全 feature を JSON でも保存。
    Returns: 該当行の id。
    """
    if decision not in ("go", "pass"):
        raise ValueError(f"decision must be go/pass, got {decision!r}")
    con = _connect(db_path)
    try:
        con.execute(
            "INSERT INTO decision_shadow "
            "(decided_at,decided_date,ticker,book,decision,proposed_yen,"
            "consensus,edge,council_reason,"
            "evs_total,cell_id,cap_pct_recommended,cap_pct_actual,"
            "sizing_source,cell_n_samples,cell_confidence,exploration_flag,"
            "evs_components_json) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT (ticker, book, decided_date) DO UPDATE SET "
            "decided_at=EXCLUDED.decided_at, decision=EXCLUDED.decision, "
            "proposed_yen=EXCLUDED.proposed_yen, consensus=EXCLUDED.consensus, "
            "edge=EXCLUDED.edge, council_reason=EXCLUDED.council_reason, "
            "evs_total=EXCLUDED.evs_total, cell_id=EXCLUDED.cell_id, "
            "cap_pct_recommended=EXCLUDED.cap_pct_recommended, "
            "cap_pct_actual=EXCLUDED.cap_pct_actual, "
            "sizing_source=EXCLUDED.sizing_source, "
            "cell_n_samples=EXCLUDED.cell_n_samples, "
            "cell_confidence=EXCLUDED.cell_confidence, "
            "exploration_flag=EXCLUDED.exploration_flag, "
            "evs_components_json=EXCLUDED.evs_components_json",
            [datetime.now(), decided_date, ticker, book, decision,
             proposed_yen, consensus, edge, council_reason,
             evs_total, cell_id, cap_pct_recommended, cap_pct_actual,
             sizing_source, cell_n_samples, cell_confidence, exploration_flag,
             evs_components_json],
        )
        rid = con.execute(
            "SELECT id FROM decision_shadow "
            "WHERE ticker=? AND book IS NOT DISTINCT FROM ? AND decided_date=?",
            [ticker, book, decided_date],
        ).fetchone()[0]
    finally:
        con.close()
    return int(rid)


def backfill_counterfactuals(db_path: str | None = None) -> int:
    """cf 未確定行に、出口が実バーで成立していれば反実仮想を埋める。

    出口未到達(still_open)は NULL のまま（即日計上禁止規約）。
    Returns: 今回確定させた行数。
    """
    con = _connect(db_path)
    filled = 0
    try:
        pending = con.execute(
            "SELECT id, ticker, decided_date FROM decision_shadow "
            "WHERE cf_computed_at IS NULL"
        ).fetchall()
        for rid, ticker, dec_date in pending:
            raw = load_bars(ticker)
            if raw is None or len(raw) < 30:
                continue
            raw = raw.copy()
            raw["date"] = pd.to_datetime(raw["date"]).dt.date
            ind = _compute_indicators(raw)
            st = _simulate_one(ind, dec_date)
            # 出口未到達 / バー不足 → 確定しない（NULLのまま＝即日計上しない）
            if st is None or st.pnl_pct is None:
                continue
            con.execute(
                "UPDATE decision_shadow SET cf_exit_date=?, cf_exit_reason=?, "
                "cf_pnl_pct=?, cf_won=?, cf_computed_at=? WHERE id=?",
                [st.exit_date, st.exit_reason, st.pnl_pct,
                 bool(st.won), datetime.now(), rid],
            )
            filled += 1
    finally:
        con.close()
    logger.info("decision_shadow backfill: %d counterfactuals confirmed", filled)
    return filled
