"""monitor_graph: 既存ポジション監視・エグジット。

Issue #7: monitor_node → exit_node。
4条件: 損切り(-5%) / 最大保有(5営業日) / 利確(MA25回帰) / 反転シグナル。
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, date
from typing import Any, TypedDict

import numpy as np
import pandas as pd
from langgraph.graph import StateGraph, END

from src.data.jquants import fetch_daily_ohlcv, calc_ma25_deviation, fetch_nikkei_change, fetch_nikkei_ma25_deviation
from src.voting import vote_all, CONSENSUS_THRESHOLD
from src.broker.tachibana import BrokerBase, MockTachibanaClient
from src.data.db import get_connection, calc_commission
from src.news_scanner import scan_and_store, pending_negatives, mark_applied
from config.strategy_params import DEFAULTS

logger = logging.getLogger("monitor_graph")

# S3: 単一ソース DEFAULTS 参照（値は現値 -0.07/15 と同一＝挙動保存）
# 2026-05-24: MAX_CONCURRENT_POSITIONS は kelly_node 内で book別管理へ移行 (commit c9254b4)
# monitor.py は同時保有上限を参照しないため削除
STOP_LOSS_PCT = DEFAULTS.stop_loss
MAX_HOLD_DAYS = DEFAULTS.max_hold_days
ALLOWED_REGIME_ONLY_BEARISH = True  # 2026-04-21: Nikkei MA25乖離 <= -3%の時のみentry


class MonitorState(TypedDict):
    open_positions: list       # [{id, ticker, entry_price, shares, entry_date}]
    exit_decisions: list       # [{id, ticker, shares, reason, current_price}]
    errors: list[str]
    db_path: str | None
    broker: Any


def _business_days_between(start_date: date, end_date: date) -> int:
    """営業日数を計算。"""
    return int(np.busday_count(start_date, end_date))


# === Nodes ===

def monitor_node(state: MonitorState) -> dict:
    """4条件でエグジット判定。"""
    positions = state.get("open_positions", [])
    db_path = state.get("db_path")
    exit_decisions = []
    errors = list(state.get("errors", []))

    if not positions:
        return {"exit_decisions": [], "errors": errors}

    # 日経225騰落率 + MA25レジーム（反転シグナル用）
    try:
        nikkei_change = fetch_nikkei_change()
    except Exception:
        nikkei_change = 0.0
    try:
        nk_ma25 = fetch_nikkei_ma25_deviation()
    except Exception:
        nk_ma25 = None

    # Phase 1 L-A: EDINET開示スキャン (当日の open銘柄 negative判定を先行検出)
    open_tickers = [p["ticker"] for p in positions]
    try:
        scan_and_store(open_tickers, db_path=db_path)
    except Exception as e:
        errors.append(f"news_scan:{e}")
    neg_map = {t: (d, r) for t, d, r in pending_negatives(open_tickers, db_path=db_path)}

    today = date.today()

    for pos in positions:
        ticker = pos["ticker"]
        entry_price = pos["entry_price"]
        shares = pos["shares"]
        entry_date = pos["entry_date"]
        if isinstance(entry_date, str):
            entry_date = date.fromisoformat(entry_date)
        pos_id = pos["id"]

        try:
            df = fetch_daily_ohlcv(ticker)
            current_price = float(df["close"].iloc[-1])
        except Exception as e:
            errors.append(f"monitor:{ticker}:{e}")
            continue

        # ⓪ news_negative 最優先exit (開示検出)
        if ticker in neg_map:
            doc_id, nreason = neg_map[ticker]
            exit_decisions.append({
                "id": pos_id, "ticker": ticker, "shares": shares,
                "reason": "news_negative", "current_price": current_price,
                "news_doc_id": doc_id, "news_reason": nreason,
            })
            try:
                mark_applied(ticker, doc_id, db_path=db_path)
            except Exception:
                pass
            continue

        # ① 損切り: -5%以下
        pnl_pct = (current_price - entry_price) / entry_price
        if pnl_pct <= STOP_LOSS_PCT:
            exit_decisions.append({
                "id": pos_id, "ticker": ticker, "shares": shares,
                "reason": "stop_loss", "current_price": current_price,
            })
            continue

        # ② 最大保有期間: 5営業日
        hold_days = _business_days_between(entry_date, today)
        if hold_days >= MAX_HOLD_DAYS:
            exit_decisions.append({
                "id": pos_id, "ticker": ticker, "shares": shares,
                "reason": "max_hold", "current_price": current_price,
            })
            continue

        # ③ 利確: MA25回帰（乖離率 >= 0）
        try:
            deviation = calc_ma25_deviation(df)
            if deviation >= 0:
                exit_decisions.append({
                    "id": pos_id, "ticker": ticker, "shares": shares,
                    "reason": "take_profit", "current_price": current_price,
                })
                continue
        except Exception:
            pass

        # ④ 反転シグナル: consensus < 3
        try:
            _loop = asyncio.new_event_loop()
            try:
                vote_result = _loop.run_until_complete(
                    vote_all(ticker, df, nikkei_change, nikkei_ma25_dev=nk_ma25, db_path=db_path)
                )
            finally:
                _loop.close()
            if vote_result["consensus"] < CONSENSUS_THRESHOLD:
                exit_decisions.append({
                    "id": pos_id, "ticker": ticker, "shares": shares,
                    "reason": "signal_reversal", "current_price": current_price,
                })
                continue
        except Exception:
            pass

    return {"exit_decisions": exit_decisions, "errors": errors}


def exit_node(state: MonitorState) -> dict:
    """売り注文実行 + DB更新。"""
    decisions = state.get("exit_decisions", [])
    broker = state.get("broker")
    db_path = state.get("db_path")
    errors = list(state.get("errors", []))

    if not broker or not decisions:
        return {"errors": errors}

    for dec in decisions:
        ticker = dec["ticker"]
        shares = dec["shares"]
        pos_id = dec["id"]
        reason = dec["reason"]
        current_price = dec["current_price"]

        try:
            # paperモード: Mockをmonitor_nodeが算出した実現在値で約定させる
            if isinstance(broker, MockTachibanaClient):
                broker.set_price(ticker, current_price)
            result = broker.sell(ticker, shares)
            if result.get("status") != "ok":
                errors.append(f"exit:{ticker}:{result.get('message', 'unknown')}")
                continue

            exit_price = result.get("executed_price", current_price)

            # DB更新
            con = get_connection(db_path)

            # entry_price + entry_commission取得してPnL計算（往復手数料控除）
            row = con.execute(
                "SELECT entry_price, commission FROM positions WHERE id = ?", [pos_id]
            ).fetchone()
            entry_price = row[0] if row else current_price
            entry_commission = row[1] if row else 0.0
            exit_commission = calc_commission(exit_price, shares)
            total_commission = entry_commission + exit_commission
            pnl = (exit_price - entry_price) * shares - total_commission

            con.execute("""
                UPDATE positions
                SET status = 'closed',
                    exit_price = ?,
                    exit_reason = ?,
                    pnl = ?,
                    closed_at = ?,
                    commission = ?
                WHERE id = ?
            """, [exit_price, reason, pnl, datetime.now(), total_commission, pos_id])

            # monthly_pnl更新
            ym = datetime.now().strftime("%Y-%m")
            existing = con.execute(
                "SELECT year_month FROM monthly_pnl WHERE year_month = ?", [ym]
            ).fetchone()

            if existing:
                con.execute("""
                    UPDATE monthly_pnl
                    SET total_pnl = total_pnl + ?
                    WHERE year_month = ?
                """, [pnl, ym])
            else:
                con.execute("""
                    INSERT INTO monthly_pnl (year_month, total_pnl, drawdown_pct, is_stopped)
                    VALUES (?, ?, 0.0, FALSE)
                """, [ym, pnl])

            con.close()
            logger.info("Sold %s x%d @%.1f reason=%s pnl=%.0f (commission=%.0f)",
                        ticker, shares, exit_price, reason, pnl, total_commission)

        except Exception as e:
            errors.append(f"exit:{ticker}:{e}")

    return {"errors": errors}


# === Routing ===

def should_exit(state: MonitorState) -> str:
    if state.get("exit_decisions"):
        return "exit_node"
    return END


# === Graph ===

def build_monitor_graph() -> StateGraph:
    graph = StateGraph(MonitorState)
    graph.add_node("monitor_node", monitor_node)
    graph.add_node("exit_node", exit_node)
    graph.set_entry_point("monitor_node")
    graph.add_conditional_edges("monitor_node", should_exit, {
        "exit_node": "exit_node",
        END: END,
    })
    graph.add_edge("exit_node", END)
    return graph.compile()


def run_monitor_cycle(
    *,
    broker: BrokerBase | None = None,
    db_path: str | None = None,
) -> MonitorState:
    """1監視サイクルを実行。"""
    # DBからopenポジション取得
    positions = []
    try:
        con = get_connection(db_path)
        rows = con.execute("""
            SELECT id, ticker, entry_price, shares, entry_date
            FROM positions WHERE status = 'open'
        """).fetchall()
        for r in rows:
            positions.append({
                "id": r[0], "ticker": r[1], "entry_price": r[2],
                "shares": r[3], "entry_date": r[4],
            })
        con.close()
    except Exception as e:
        logger.error("Failed to load positions: %s", e)

    app = build_monitor_graph()
    initial_state: MonitorState = {
        "open_positions": positions,
        "exit_decisions": [],
        "errors": [],
        "db_path": db_path,
        "broker": broker,
    }
    return app.invoke(initial_state)
