"""monitor_graph: 既存ポジション監視・エグジット (令和式 BNF 2026-05-25改修)。

7条件 (priority high→low):
  0. news_negative (EDINET 開示 negative)
  1. gap_down_stop (寄付き値 entry-3% 以下で即exit、stop_loss が gap downで -8%にずれる問題緩和)
  2. stop_loss (現価-7%以下、stop_loss_pct)
  3. take_profit (MA25乖離 >= +2%、Honda asymmetric_tp)
  4. trailing_stop (max +1.5%到達後、peak から -1% で逃げる、+2.69% max 捕捉)
  5. max_hold (15営業日)
  6. signal_reversal (consensus<3、最終 fallback に降格)

2026-05-25 改修根拠 (counterfactual分析):
  - 旧設計: take_profit は signal_reversal が先勝ちで 0件発火 (consensus<3 が必ず早い)
  - 旧設計: stop_loss -7%設計だが gap で -7~-8.5% にずれていた (10件全敗の主因)
  - 旧設計: trailing なし → max +2.69% avg 取り逃し (exit +0.91%、35% capture率)
  - 修正: take_profit priority格上げ + asymmetric (+2%) で 31% positions が +1pp改善
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

# S3: 単一ソース DEFAULTS 参照
STOP_LOSS_PCT = DEFAULTS.stop_loss
MAX_HOLD_DAYS = DEFAULTS.max_hold_days
# 令和式 exit params (2026-05-25)
TAKE_PROFIT_DEV = DEFAULTS.take_profit_dev  # MA25乖離率 >= +0.02 で利確
TRAILING_ACTIVATION_PCT = DEFAULTS.trailing_activation_pct  # +1.5% touched で活性化
TRAILING_DRAWDOWN_PCT = DEFAULTS.trailing_drawdown_pct      # peak から -1% で exit
GAP_DOWN_STOP_PCT = DEFAULTS.gap_down_stop_pct              # 寄付き-3%以下で即exit
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
        max_price_seen = pos.get("max_price_seen") or entry_price

        try:
            df = fetch_daily_ohlcv(ticker)
            current_price = float(df["close"].iloc[-1])
            today_open = float(df["open"].iloc[-1]) if "open" in df.columns else current_price
        except Exception as e:
            errors.append(f"monitor:{ticker}:{e}")
            continue

        # max_price_seen 更新 (trailing stop用) - DB write
        new_max = max(float(max_price_seen), current_price)
        if new_max > float(max_price_seen):
            try:
                con = get_connection(db_path)
                con.execute(
                    "UPDATE positions SET max_price_seen = ? WHERE id = ?",
                    [new_max, pos_id],
                )
                con.close()
            except Exception as e:
                errors.append(f"max_price_update:{ticker}:{e}")
        max_price_seen = new_max

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

        # ① 寄付きgap_down stop (令和式 2026-05-25): 寄付きが entry の -3% 以下で即exit
        # 旧 stop_loss -7% は gap で -7~-8.5% にずれていた問題への対処
        gap_pct = (today_open - entry_price) / entry_price
        if gap_pct <= GAP_DOWN_STOP_PCT:
            exit_decisions.append({
                "id": pos_id, "ticker": ticker, "shares": shares,
                "reason": "gap_down_stop", "current_price": today_open,
            })
            logger.info("[gap_down] %s open %.1f vs entry %.1f (%.2f%%)",
                        ticker, today_open, entry_price, gap_pct*100)
            continue

        # ② 損切り: -7%以下 (current close base)
        pnl_pct = (current_price - entry_price) / entry_price
        if pnl_pct <= STOP_LOSS_PCT:
            exit_decisions.append({
                "id": pos_id, "ticker": ticker, "shares": shares,
                "reason": "stop_loss", "current_price": current_price,
            })
            continue

        # ③ take_profit 優先化 (令和式 Honda asymmetric_tp): MA25乖離 >= +2%
        # 旧: dev>=0、かつ signal_reversal の後で発火 0件
        # 新: dev>=TAKE_PROFIT_DEV(+0.02)、signal_reversal より前で評価
        try:
            deviation = calc_ma25_deviation(df)
            if deviation >= TAKE_PROFIT_DEV:
                exit_decisions.append({
                    "id": pos_id, "ticker": ticker, "shares": shares,
                    "reason": "take_profit", "current_price": current_price,
                })
                continue
        except Exception:
            pass

        # ④ trailing_stop (令和式): max +1.5%到達後、peak から -1% drawdown で exit
        # max +2.69% avg 取り逃し問題への対処、35% capture → 60%目標
        trailing_active = max_price_seen >= entry_price * (1 + TRAILING_ACTIVATION_PCT)
        if trailing_active:
            trailing_trigger = max_price_seen * (1 - TRAILING_DRAWDOWN_PCT)
            if current_price <= trailing_trigger:
                exit_decisions.append({
                    "id": pos_id, "ticker": ticker, "shares": shares,
                    "reason": "trailing_stop", "current_price": current_price,
                })
                pnl_locked = (current_price - entry_price) / entry_price * 100
                logger.info("[trailing] %s max %.1f → %.1f (locked %.2f%%, entry %.1f)",
                            ticker, max_price_seen, current_price, pnl_locked, entry_price)
                continue

        # ⑤ 最大保有期間: 15営業日
        hold_days = _business_days_between(entry_date, today)
        if hold_days >= MAX_HOLD_DAYS:
            exit_decisions.append({
                "id": pos_id, "ticker": ticker, "shares": shares,
                "reason": "max_hold", "current_price": current_price,
            })
            continue

        # ⑥ 反転シグナル: consensus < 3 (令和式: 最終 fallback に降格)
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
        # max_price_seen がNULLなら entry_price で initialize (backfill)
        con.execute(
            "UPDATE positions SET max_price_seen = entry_price "
            "WHERE status='open' AND max_price_seen IS NULL"
        )
        rows = con.execute("""
            SELECT id, ticker, entry_price, shares, entry_date, max_price_seen
            FROM positions WHERE status = 'open'
        """).fetchall()
        for r in rows:
            positions.append({
                "id": r[0], "ticker": r[1], "entry_price": r[2],
                "shares": r[3], "entry_date": r[4],
                "max_price_seen": r[5] if r[5] is not None else r[2],
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
