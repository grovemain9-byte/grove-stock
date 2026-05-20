"""scan_graph: LangGraphパイプライン。

Issue #6: ingestion → voting → kelly → execution。
"""
from __future__ import annotations

import asyncio
import logging
import os
from datetime import date, datetime
from typing import Annotated, Any, TypedDict

from langgraph.graph import StateGraph, END

from src.data.jquants import TICKERS, fetch_daily_ohlcv, fetch_nikkei_change, fetch_nikkei_ma25_deviation, calc_ma25_deviation
from src.data.cache import load_universe, load_bars
from src.voting import vote_all, CONSENSUS_THRESHOLD
from src.kelly import kelly_size
from src.broker.tachibana import BrokerBase, MockTachibanaClient
from src.data.db import get_connection, calc_commission, book_account
from src.measurement.decision_shadow import record_proposal
from config.books import BOOKS, LEGACY_BOOK
from config.strategy_params import DEFAULTS

logger = logging.getLogger("scan_graph")


# === State ===

class ScanState(TypedDict):
    market_data: dict          # ticker → DataFrame
    nikkei_change: float
    nikkei_ma25_dev: float | None
    votes: dict                # ticker → {p1..p5, consensus}
    position_size: dict        # ticker → int (shares)
    monthly_dd_exceeded: bool
    errors: list[str]
    db_path: str | None
    broker: Any                # BrokerBase instance


# === Helpers ===

def filter_signals(votes: dict) -> dict:
    """consensus≥閾値 の銘柄だけ抽出（投票サマリ・ルーティング共通）。"""
    return {t: v for t, v in votes.items() if v.get("consensus", 0) >= CONSENSUS_THRESHOLD}


def _cap_shares_by_cash(shares: int, price: float, cash: float) -> int:
    """price*shares + 片道手数料 が cash 以内に収まる最大の100株単位。

    book_account の committed は手数料込み＝対称。kelly_size が返す大きな値から
    decrement するのでなく cash 上界から始め、手数料超過の最大1-2単元のみ削る
    （実質O(1)。¥50Mブックで while が数百回回る C1 を解消）。
    """
    affordable = int(cash / price / 100) * 100 if price > 0 else 0
    s = min(shares, max(affordable, 0))
    while s > 0 and s * price + calc_commission(price, s) > cash:
        s -= 100
    return s


# === Nodes ===

def ingestion_node(state: ScanState) -> dict:
    """TOPIX500 494銘柄の日足OHLCV (cache経由) + 日経225騰落率を取得。
    cacheは前夜のdaily_updateで更新済みの前提。未更新なら古いデータで判定される。
    """
    market_data = {}
    errors = list(state.get("errors", []))

    try:
        universe = load_universe()
    except Exception as e:
        errors.append(f"ingestion:universe:{e}")
        universe = None

    if universe is not None and len(universe) > 0:
        # ticker = Code (5桁)。vote_allはstrでマッチするから5桁のまま
        codes = list(universe["Code"])
        for code in codes:
            try:
                df = load_bars(code)
                if len(df) < 25:
                    continue
                # Schema合わせ (loadはpd DataFrame with date,open,high,low,close,volume,value)
                market_data[code] = df
            except Exception as e:
                errors.append(f"ingestion:{code}:{str(e)[:50]}")
        logger.info("ingestion: loaded %d/%d from cache", len(market_data), len(codes))
    else:
        # Fallback: 13銘柄直接J-Quants取得
        for ticker in TICKERS:
            try:
                market_data[ticker] = fetch_daily_ohlcv(ticker)
            except Exception as e:
                errors.append(f"ingestion:{ticker}:{e}")
                logger.error("Ingestion failed for %s: %s", ticker, e)

    try:
        nikkei = fetch_nikkei_change()
    except Exception as e:
        nikkei = 0.0
        errors.append(f"ingestion:nikkei:{e}")

    try:
        nikkei_ma25 = fetch_nikkei_ma25_deviation()
    except Exception as e:
        nikkei_ma25 = None
        errors.append(f"ingestion:nikkei_ma25:{e}")

    return {"market_data": market_data, "nikkei_change": nikkei,
            "nikkei_ma25_dev": nikkei_ma25, "errors": errors}


def voting_node(state: ScanState) -> dict:
    """全銘柄の投票を実行。"""
    market_data = state.get("market_data", {})
    nikkei = state.get("nikkei_change", 0.0)
    nk_ma25 = state.get("nikkei_ma25_dev")
    db_path = state.get("db_path")
    votes = {}
    errors = list(state.get("errors", []))

    # 接続をループ外で1回（scans書込を共有接続でバッチ化＝494回open+DDLを1回に）。
    # 失敗時は con=None で従来の銘柄毎open/closeにフォールバック（graceful）。
    con = None
    try:
        con = get_connection(db_path)
    except Exception as e:
        errors.append(f"voting:connect:{e}")
        con = None

    try:
        for ticker, df in market_data.items():
            try:
                loop = asyncio.new_event_loop()
                try:
                    result = loop.run_until_complete(
                        vote_all(ticker, df, nikkei, nikkei_ma25_dev=nk_ma25,
                                 db_path=db_path, con=con)
                    )
                finally:
                    loop.close()
                votes[ticker] = result
            except Exception as e:
                errors.append(f"voting:{ticker}:{e}")
                votes[ticker] = {"p1": False, "p2": False, "p3": False, "p4": False, "p5": False, "consensus": 0}
    finally:
        if con is not None:
            con.close()

    return {"votes": votes, "errors": errors}


MAX_CONCURRENT_POSITIONS = DEFAULTS.max_concurrent  # S3: 単一ソース参照（現値7と同一）


def kelly_node(state: ScanState) -> dict:
    """コンセンサス≥3 + 同時保有上限でKellyサイジング。

    2026-05-12: 12セルgrid再測 → p4必須 & bearish gate は過剰防衛と判明、撤去。
    Pareto優位: c=3 + p4_optional + regime gateなし で Sharpe 2.19 / Calmar 13.3 / +213%/年。

    マルチブック (2026-05-16): state に book_equity/book_free_cash/book_flex/
    book_open_tickers があればブック会計で配分。無ければ従来挙動（dry-run/live/
    既存テスト完全維持）。free_cash を累積管理し over-deploy を防ぐ。
    """
    votes = state.get("votes", {})
    broker = state.get("broker")
    db_path = state.get("db_path")
    position_size = {}
    errors = list(state.get("errors", []))

    book = state.get("book")
    if book is not None:
        # マルチブック経路: ブック会計済みの値を使う
        equity = state.get("book_equity", 0.0)
        free_cash = state.get("book_free_cash", 0.0)
        flex = state.get("book_flex", False)
        regime_filter = state.get("book_regime_filter", False)
        existing_tickers = set(state.get("book_open_tickers", set()))
    else:
        # 従来経路（dry-run/live/既存テスト）: 挙動不変
        equity = 1_000_000.0
        if broker:
            try:
                equity = broker.get_balance()
            except Exception as e:
                errors.append(f"kelly:balance:{e}")
        free_cash = equity
        flex = False
        regime_filter = False  # 従来経路は regime_filter 無し（挙動不変）
        existing_tickers = set()
        try:
            con = get_connection(db_path)
            rows = con.execute("SELECT ticker FROM positions WHERE status = 'open'").fetchall()
            existing_tickers = {r[0] for r in rows}
            con.close()
        except Exception:
            pass

    today = date.today()
    market_data = state.get("market_data", {})

    # 同時保有上限チェック
    open_count = len(existing_tickers)
    if open_count >= MAX_CONCURRENT_POSITIONS:
        logger.info("skip kelly: max concurrent positions (%d) reached", MAX_CONCURRENT_POSITIONS)
        # A/B評価のため、consensus≥3 の判断は枠制約理由で pass 記録する
        for ticker, vote_result in votes.items():
            consensus = vote_result.get("consensus", 0)
            if consensus < CONSENSUS_THRESHOLD:
                continue
            try:
                record_proposal(
                    ticker=ticker, decision="pass", decided_date=today,
                    book=book, consensus=consensus, edge=vote_result.get("dev"),
                    council_reason="max_concurrent_full", db_path=db_path,
                )
            except Exception as e:
                errors.append(f"decision_shadow:{ticker}:{e}")
        return {"position_size": {}, "errors": errors}

    slots_left = MAX_CONCURRENT_POSITIONS - open_count
    remaining_cash = free_cash
    for ticker, vote_result in votes.items():
        if len(position_size) >= slots_left:
            break  # slots_full は記録しない（A/B比較と無関係）
        consensus = vote_result.get("consensus", 0)
        if consensus < CONSENSUS_THRESHOLD:
            continue
        edge = vote_result.get("dev")

        # A/B: treatment ブックは弱気レジーム(p4)時のみ建玉（regime_filter）。
        # control ブックは従来通り consensus≥閾値で建玉。
        if regime_filter and not vote_result.get("p4"):
            try:
                record_proposal(
                    ticker=ticker, decision="pass", decided_date=today,
                    book=book, consensus=consensus, edge=edge,
                    council_reason="regime_filter_skip:p4=False", db_path=db_path,
                )
            except Exception as e:
                errors.append(f"decision_shadow:{ticker}:{e}")
            continue
        if ticker in existing_tickers:
            logger.info("Skip %s: already has open position", ticker)
            try:
                record_proposal(
                    ticker=ticker, decision="pass", decided_date=today,
                    book=book, consensus=consensus, edge=edge,
                    council_reason="already_open", db_path=db_path,
                )
            except Exception as e:
                errors.append(f"decision_shadow:{ticker}:{e}")
            continue

        df = market_data.get(ticker)
        if df is None or df.empty:
            try:
                record_proposal(
                    ticker=ticker, decision="pass", decided_date=today,
                    book=book, consensus=consensus, edge=edge,
                    council_reason="no_market_data", db_path=db_path,
                )
            except Exception as e:
                errors.append(f"decision_shadow:{ticker}:{e}")
            continue

        price = float(df["close"].iloc[-1])
        # サイジング規律の母数 = equity（初期+実現損益。trading-rules「対bankroll%」）。
        # それを free_cash 残（手数料込み）でハードキャップ＝over-deploy防止。
        shares = kelly_size(consensus, equity, price, flex=flex)
        shares = _cap_shares_by_cash(shares, price, remaining_cash)
        if shares > 0:
            position_size[ticker] = shares
            remaining_cash -= shares * price + calc_commission(price, shares)
            try:
                record_proposal(
                    ticker=ticker, decision="go", decided_date=today,
                    book=book, consensus=consensus, edge=edge,
                    proposed_yen=shares * price,
                    council_reason="kelly_ok", db_path=db_path,
                )
            except Exception as e:
                errors.append(f"decision_shadow:{ticker}:{e}")
        else:
            try:
                record_proposal(
                    ticker=ticker, decision="pass", decided_date=today,
                    book=book, consensus=consensus, edge=edge,
                    council_reason="shares_zero", db_path=db_path,
                )
            except Exception as e:
                errors.append(f"decision_shadow:{ticker}:{e}")

    return {"position_size": position_size, "errors": errors}


def execution_node(state: ScanState) -> dict:
    """注文実行 + DB記録。"""
    position_size = state.get("position_size", {})
    broker = state.get("broker")
    market_data = state.get("market_data", {})
    db_path = state.get("db_path")
    errors = list(state.get("errors", []))

    if not broker or not position_size:
        return {"errors": errors}

    book = state.get("book")
    if book is None:
        book = LEGACY_BOOK

    # DuckDB接続はループ外で1回（建玉毎open/closeはロック競合を自作する）
    con = get_connection(db_path)
    try:
        for ticker, shares in position_size.items():
            try:
                # paperモード: Mock既定¥1000 → 実際の足の終値で約定させる
                if isinstance(broker, MockTachibanaClient):
                    df = market_data.get(ticker)
                    if df is not None and not df.empty:
                        broker.set_price(ticker, float(df["close"].iloc[-1]))
                result = broker.buy(ticker, shares)
                if result.get("status") == "ok":
                    price = result.get("executed_price", 0.0)
                    entry_commission = calc_commission(price, shares)
                    con.execute("""
                        INSERT INTO positions (ticker, entry_price, shares, entry_date, status, commission, book)
                        VALUES (?, ?, ?, ?, 'open', ?, ?)
                    """, [ticker, price, shares, datetime.now().date(), entry_commission, book])
                    logger.info("[%s] Bought %s x%d @%.1f commission=%.0f",
                                book, ticker, shares, price, entry_commission)
                else:
                    errors.append(f"execution:{ticker}:{result.get('message', 'unknown')}")
            except Exception as e:
                errors.append(f"execution:{ticker}:{e}")
    finally:
        con.close()

    return {"errors": errors}


# === Routing ===

def should_continue(state: ScanState) -> str:
    """consensus≥3が1銘柄以上あり、DD未超過ならkellyへ。"""
    if state.get("monthly_dd_exceeded", False):
        return END

    votes = state.get("votes", {})
    has_signal = bool(filter_signals(votes))

    if not has_signal:
        return END

    return "kelly_node"


# === Graph ===

def build_scan_graph() -> StateGraph:
    """scan_graphを構築。"""
    graph = StateGraph(ScanState)

    graph.add_node("ingestion_node", ingestion_node)
    graph.add_node("voting_node", voting_node)
    graph.add_node("kelly_node", kelly_node)
    graph.add_node("execution_node", execution_node)

    graph.set_entry_point("ingestion_node")
    graph.add_edge("ingestion_node", "voting_node")
    graph.add_conditional_edges("voting_node", should_continue, {
        "kelly_node": "kelly_node",
        END: END,
    })
    graph.add_edge("kelly_node", "execution_node")
    graph.add_edge("execution_node", END)

    return graph.compile()


def run_scan_cycle(
    *,
    broker: BrokerBase | None = None,
    db_path: str | None = None,
    monthly_dd_exceeded: bool = False,
) -> ScanState:
    """1��キャンサイクルを実行。"""
    if broker is None:
        broker = MockTachibanaClient()

    app = build_scan_graph()
    initial_state: ScanState = {
        "market_data": {},
        "nikkei_change": 0.0,
        "nikkei_ma25_dev": None,
        "votes": {},
        "position_size": {},
        "monthly_dd_exceeded": monthly_dd_exceeded,
        "errors": [],
        "db_path": db_path,
        "broker": broker,
    }

    result = app.invoke(initial_state)
    return result


def run_paper_multibook(db_path: str | None = None) -> dict:
    """実価格ペーパー・マルチブック。

    ingestion+voting は資金非依存なので1回だけ実行（scansも1回記録）。
    その votes を 5ブック(¥1M〜¥5000万) それぞれの資金会計で kelly+execution。
    monitor は全ブック横断で1回（book列は行に付随しUPDATEで保存される）。
    """
    from src.monitor import run_monitor_cycle

    logger.info("=== scan_graph 開始 (paper multibook) ===")
    base: ScanState = {
        "market_data": {}, "nikkei_change": 0.0, "nikkei_ma25_dev": None,
        "votes": {}, "position_size": {}, "monthly_dd_exceeded": False,
        "errors": [], "db_path": db_path, "broker": None,
    }
    s = {**base, **ingestion_node(base)}
    s = {**s, **voting_node(s)}  # scans を1回だけ記録

    votes = s.get("votes", {})
    signals = filter_signals(votes)
    logger.info("スキャン完了: %d銘柄, シグナル%d件", len(votes), len(signals))

    all_errors = list(s.get("errors", []))
    for bk in BOOKS:
        try:
            con = get_connection(db_path)
            equity, free_cash, open_tk = book_account(con, bk.book_id, bk.initial_capital)
            con.close()
        except Exception as e:
            all_errors.append(f"book_account:{bk.book_id}:{e}")
            continue

        # free_cash<0 = 実現損が初期資金を侵食。サイレントclamp禁止（異常サイン）。
        if free_cash < 0:
            msg = f"book_account:{bk.book_id}:negative_free_cash={free_cash:.0f}"
            logger.warning("[%s] free_cash<0 (%.0f) — ブックを停止", bk.book_id, free_cash)
            all_errors.append(msg)
            continue

        bstate: ScanState = {
            **s,
            "broker": MockTachibanaClient(initial_balance=free_cash),
            "book": bk.book_id,
            "book_equity": equity,
            "book_free_cash": free_cash,
            "book_flex": bk.flex,
            "book_regime_filter": bk.regime_filter,
            "book_open_tickers": open_tk,
        }
        ks = kelly_node(bstate)
        bstate.update(ks)
        ex = execution_node(bstate)
        all_errors += ex.get("errors", [])

        pos = ks.get("position_size", {})
        logger.info("[%s] equity=%.0f free=%.0f flex=%s 注文%d件",
                    bk.book_id, equity, free_cash, bk.flex, len(pos))

    # monitor は全ブック横断で1回
    logger.info("=== monitor_graph 開始 ===")
    mon = run_monitor_cycle(broker=MockTachibanaClient(), db_path=db_path)
    exits = mon.get("exit_decisions", [])
    logger.info("  エグジット%d件", len(exits))
    all_errors += mon.get("errors", [])

    if all_errors:
        logger.warning("エラー %d件:", len(all_errors))
        for err in all_errors[:5]:
            logger.warning("  %s", err)
    return {"votes": votes, "errors": all_errors, "exits": exits}


# === CLI ===

def main():
    import argparse
    import json
    from pathlib import Path
    from dotenv import load_dotenv

    load_dotenv(Path(__file__).resolve().parent.parent / ".env")

    parser = argparse.ArgumentParser(description="grove-stock scan cycle")
    parser.add_argument("--dry-run", action="store_true",
                        help="MockClient+ダミー価格（スモーク用、注文しない）")
    parser.add_argument("--paper", action="store_true",
                        help="実価格ペーパー: 立花ログイン不要、Mockに実足終値で約定させ"
                             "シグナルをpositionsに記録する（本番発注しない）")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    db_path = str(Path(__file__).resolve().parent.parent / "data" / "grove_stock.duckdb")

    initial_balance = float(os.getenv("INITIAL_BALANCE", "1000000"))

    if args.paper:
        # 実価格ペーパー・マルチブック: 立花ログイン不要。scan/voting 1回 →
        # 5ブック(¥1M〜¥5000万)それぞれの資金会計で建玉 → monitor 1回。
        logger.info("[PAPER] 実価格ペーパー・マルチブック（本番発注なし）")
        run_paper_multibook(db_path=db_path)
        return
    if args.dry_run:
        broker = MockTachibanaClient(initial_balance=initial_balance)
        # 全銘柄にダミー価格設定（Mockのbuy/sell用）
        for t in TICKERS:
            broker.set_price(t, 1000.0)
        logger.info("[DRY-RUN] MockClient使用")
    else:
        from src.broker.tachibana import TachibanaClient
        broker = TachibanaClient()
        if not broker.login():
            logger.error("立花証券ログイン失敗")
            return
        logger.info("立花証券ログイン成功")

    # scan cycle
    logger.info("=== scan_graph 開始 ===")
    scan_result = run_scan_cycle(broker=broker, db_path=db_path)

    # 投票サマリー
    votes = scan_result.get("votes", {})
    signals = filter_signals(votes)
    logger.info("スキャン完了: %d銘柄, シグナル%d件", len(votes), len(signals))
    for t, v in signals.items():
        logger.info("  BUY %s consensus=%d", t, v["consensus"])

    # position_size
    pos = scan_result.get("position_size", {})
    if pos:
        for t, s in pos.items():
            logger.info("  注文: %s x%d", t, s)
    else:
        logger.info("  注文なし")

    # monitor cycle
    from src.monitor import run_monitor_cycle
    logger.info("=== monitor_graph 開始 ===")
    mon_result = run_monitor_cycle(broker=broker, db_path=db_path)

    exits = mon_result.get("exit_decisions", [])
    if exits:
        for e in exits:
            logger.info("  EXIT %s reason=%s", e["ticker"], e["reason"])
    else:
        logger.info("  エグジットなし")

    # エラー表示
    all_errors = scan_result.get("errors", []) + mon_result.get("errors", [])
    if all_errors:
        logger.warning("エラー %d件:", len(all_errors))
        for err in all_errors[:5]:
            logger.warning("  %s", err)

    if not args.dry_run and hasattr(broker, "logout"):
        broker.logout()


if __name__ == "__main__":
    main()
