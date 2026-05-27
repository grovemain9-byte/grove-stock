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
from src.sizing.evs import compute_evs, EVSComponents
from src.sizing.router import decide_cap, shares_from_decision, SizingDecision
from config.books import BOOKS, LEGACY_BOOK
from config.sector_config import get_threshold
from config.strategy_params import DEFAULTS

import json
import math

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


# --- Phase 0+ EVS/PLT helpers (2026-05-21) ---

def _extract_indicators(df) -> dict:
    """df から RSI(14), BB lower(25,2), volumes, realized_vol_20d, avg_volume_20d を抽出.

    返り値はすべて None セーフ。指標計算失敗時は該当キーに None。
    """
    import pandas as pd
    out: dict = {
        "rsi": None,
        "bb_lower": None,
        "close": None,
        "volumes_3day": None,
        "realized_vol_20d": None,
        "avg_volume_20d": None,
    }
    if df is None or len(df) < 25:
        return out
    try:
        close = df["close"].astype(float)
        out["close"] = float(close.iloc[-1])
        # RSI(14)
        try:
            import ta.momentum
            rsi_series = ta.momentum.RSIIndicator(close, window=DEFAULTS.rsi_period).rsi()
            v = rsi_series.iloc[-1]
            if not pd.isna(v):
                out["rsi"] = float(v)
        except Exception:
            pass
        # BB(25, 2)
        try:
            import ta.volatility
            bb = ta.volatility.BollingerBands(
                close, window=DEFAULTS.bb_period, window_dev=DEFAULTS.bb_std,
            )
            lower = bb.bollinger_lband().iloc[-1]
            if not pd.isna(lower):
                out["bb_lower"] = float(lower)
        except Exception:
            pass
        # Volumes
        if "volume" in df.columns and len(df) >= 3:
            volume = df["volume"].astype(float)
            try:
                v3, v2, v1 = float(volume.iloc[-3]), float(volume.iloc[-2]), float(volume.iloc[-1])
                out["volumes_3day"] = (v3, v2, v1)
            except Exception:
                pass
            if len(volume) >= 20:
                try:
                    out["avg_volume_20d"] = float(volume.tail(20).mean())
                except Exception:
                    pass
        # Realized vol (20d, annualized)
        if len(close) >= 21:
            try:
                rets = close.pct_change().dropna().tail(20)
                if len(rets) >= 5:
                    out["realized_vol_20d"] = float(rets.std() * math.sqrt(252))
            except Exception:
                pass
    except Exception:
        pass
    return out


def _sector_winrate_from_db(db_path: str | None, sector: str) -> tuple[int, int]:
    """sector 別の (n_wins, n_losses) を positions(closed) から集計.

    sector="other" の場合は全 closed の集計 (母集団 prior 代わり)。
    """
    from src.sizing.plt import TICKER_SECTORS
    try:
        con = get_connection(db_path)
        try:
            if sector == "other":
                rows = con.execute(
                    "SELECT exit_price, entry_price FROM positions "
                    "WHERE status='closed' AND exit_price IS NOT NULL AND entry_price > 0"
                ).fetchall()
            else:
                # sector に該当する ticker のみ
                target_tickers = [t for t, s in TICKER_SECTORS.items() if s == sector]
                if not target_tickers:
                    return 0, 0
                placeholders = ",".join("?" * len(target_tickers))
                rows = con.execute(
                    f"SELECT exit_price, entry_price FROM positions "
                    f"WHERE status='closed' AND exit_price IS NOT NULL "
                    f"AND entry_price > 0 AND ticker IN ({placeholders})",
                    target_tickers,
                ).fetchall()
        finally:
            con.close()
        wins = sum(1 for ex, en in rows if ex > en)
        losses = sum(1 for ex, en in rows if ex <= en)
        return wins, losses
    except Exception:
        return 0, 0


def _held_sectors_count(held_tickers: set[str]) -> dict[str, int]:
    """open positions の sector ヒストグラム."""
    from src.sizing.plt import assign_sector
    counts: dict[str, int] = {}
    for t in held_tickers:
        s = assign_sector(t)
        counts[s] = counts.get(s, 0) + 1
    return counts


def _build_evs_for_record(
    *,
    ticker: str,
    df,
    vote_result: dict,
    nikkei_ma25_dev: float | None,
    held_tickers: set[str],
    db_path: str | None,
) -> EVSComponents | None:
    """EVS を計算 (record_proposal v2 引数用). df が None / 不足なら None."""
    from src.sizing.plt import assign_sector
    ind = _extract_indicators(df)
    if ind["close"] is None:
        return None
    sector = assign_sector(ticker)
    wins, losses = _sector_winrate_from_db(db_path, sector)
    return compute_evs(
        consensus=vote_result.get("consensus", 0),
        actual_dev=vote_result.get("dev"),
        sector_threshold=get_threshold(ticker),
        rsi=ind["rsi"],
        close=ind["close"],
        bb_lower=ind["bb_lower"],
        volumes_3day=ind["volumes_3day"],
        sector_wins=wins,
        sector_losses=losses,
        nikkei_ma25_dev=nikkei_ma25_dev,
        realized_vol_20d=ind["realized_vol_20d"],
        avg_volume_20d=ind["avg_volume_20d"],
        target_ticker=ticker,
        target_sector=sector,
        held_tickers=held_tickers,
        held_sectors=_held_sectors_count(held_tickers),
    )


def _record_with_evs(
    *,
    ticker: str,
    decision: str,
    today: date,
    book: str | None,
    consensus: int,
    edge: float | None,
    council_reason: str,
    db_path: str | None,
    proposed_yen: float | None = None,
    evs_comp: EVSComponents | None = None,
    sizing_decision: SizingDecision | None = None,
) -> None:
    """record_proposal の v2 ラッパー — EVS と router decision の有無に応じて記録."""
    kwargs: dict = dict(
        ticker=ticker, decision=decision, decided_date=today,
        book=book, consensus=consensus, edge=edge,
        council_reason=council_reason, db_path=db_path,
        proposed_yen=proposed_yen,
    )
    if evs_comp is not None:
        kwargs["evs_total"] = evs_comp.evs_total
        kwargs["cap_pct_recommended"] = evs_comp.cap_pct
        kwargs["evs_components_json"] = json.dumps(evs_comp.as_dict())
    if sizing_decision is not None:
        kwargs["cell_id"] = sizing_decision.cell_id
        kwargs["cap_pct_actual"] = sizing_decision.cap_pct
        kwargs["sizing_source"] = sizing_decision.source
        kwargs["cell_n_samples"] = sizing_decision.cell_n_samples
        kwargs["cell_confidence"] = sizing_decision.cell_confidence
        kwargs["exploration_flag"] = sizing_decision.exploration_flag
    record_proposal(**kwargs)


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
        max_concurrent = int(state.get("book_max_concurrent") or MAX_CONCURRENT_POSITIONS)
        existing_tickers = set(state.get("book_open_tickers", set()))
        # 令和式 per-book playbook (Layer 7/8, 2026-05-25, 9 removed 2026-05-27)
        book_price_min = float(state.get("book_price_min") or 0.0)
        book_price_max = float(state.get("book_price_max") or 1e9)
        book_consensus_min = int(state.get("book_consensus_min_override") or CONSENSUS_THRESHOLD)
        # Phase A Boyd cushion (2026-05-27): sizing_multiplier (≤ 1.0)
        # run_paper_multibook が drawdown を計測して算出済み、ここで cap_pct に掛ける
        boyd_cushion = float(state.get("book_boyd_cushion") or 1.0)
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
        max_concurrent = MAX_CONCURRENT_POSITIONS  # 従来挙動を保存
        existing_tickers = set()
        # 令和式: legacy 経路は filter 無効化 (旧挙動保存)
        book_price_min = 0.0
        book_price_max = 1e9
        book_consensus_min = CONSENSUS_THRESHOLD
        boyd_cushion = 1.0  # legacy: cushion 無効 (full size)
        try:
            con = get_connection(db_path)
            rows = con.execute("SELECT ticker FROM positions WHERE status = 'open'").fetchall()
            existing_tickers = {r[0] for r in rows}
            con.close()
        except Exception:
            pass

    today = date.today()
    market_data = state.get("market_data", {})
    nikkei_ma25_dev = state.get("nikkei_ma25_dev")

    # consensus DESC ソート: 強シグナルを先にfillし、弱シグナルに席を奪われない
    # (5/17 拡大ユニバースで consensus=5 が全book max_concurrent_full で捨てられた問題の修正)
    sorted_votes = sorted(
        votes.items(),
        key=lambda kv: (kv[1].get("consensus", 0), kv[1].get("dev") or 0.0),
        reverse=True,
    )

    # 同時保有上限チェック (book別)
    open_count = len(existing_tickers)
    if open_count >= max_concurrent:
        logger.info("skip kelly: max concurrent positions (%d) reached", max_concurrent)
        # A/B評価のため、consensus≥3 の判断は枠制約理由で pass 記録する
        for ticker, vote_result in sorted_votes:
            consensus = vote_result.get("consensus", 0)
            if consensus < CONSENSUS_THRESHOLD:
                continue
            df = market_data.get(ticker)
            evs_comp = _build_evs_for_record(
                ticker=ticker, df=df, vote_result=vote_result,
                nikkei_ma25_dev=nikkei_ma25_dev,
                held_tickers=existing_tickers, db_path=db_path,
            )
            try:
                _record_with_evs(
                    ticker=ticker, decision="pass", today=today,
                    book=book, consensus=consensus, edge=vote_result.get("dev"),
                    council_reason="max_concurrent_full", db_path=db_path,
                    evs_comp=evs_comp,
                )
            except Exception as e:
                errors.append(f"decision_shadow:{ticker}:{e}")
        return {"position_size": {}, "errors": errors}

    slots_left = max_concurrent - open_count
    remaining_cash = free_cash
    # PLT router 用 DB 接続を1つだけ確保 (ループ毎に開くより高速)
    router_con = None
    try:
        router_con = get_connection(db_path)
    except Exception as e:
        errors.append(f"router_con:{e}")

    # F9 (concentration penalty) を「同scan内で追加されたtickerも含めて」累積するため
    # ループ用 running set を別途持つ (2026-05-25 bug fix)。
    # 旧バグ: existing_tickers は scan前open しか含まず、同scan内で18件同セクター
    # 入っても F9 fires せず、p50m 19250+85930で資金70%集中の温床になっていた。
    running_held_tickers: set[str] = set(existing_tickers)

    for ticker, vote_result in sorted_votes:
        if len(position_size) >= slots_left:
            break  # slots_full は記録しない（A/B比較と無関係）
        consensus = vote_result.get("consensus", 0)
        # 令和式 Layer 8: per-book consensus override (consensus_min_override)
        if consensus < book_consensus_min:
            continue
        # Phase 0 base check 残しておく (defense-in-depth)
        if consensus < CONSENSUS_THRESHOLD:
            continue
        edge = vote_result.get("dev")
        df = market_data.get(ticker)

        # EVS を最初に計算（pass経路でも記録するため）
        # held_tickers = running_held_tickers (同scan内累積) で F9 を正しく動作させる
        evs_comp = _build_evs_for_record(
            ticker=ticker, df=df, vote_result=vote_result,
            nikkei_ma25_dev=nikkei_ma25_dev,
            held_tickers=running_held_tickers, db_path=db_path,
        )

        # Layer 9 cross-book dedup は 2026-05-27 削除済み
        # 削除理由: A/B paper 独立性回復 (Grove vision「5 book並列独立」、
        # kabu commission ¥0 で commission節約理由消滅、LIVE は 1-account自然dedup)

        # 令和式 Layer 7: per-book price band filter + universal SKIP_PRICE_RANGES
        # 注意: book_price_min > 0 の場合のみ filter適用 (令和式 playbook explicitly configured)
        # legacy/test path (book_price_min=0.0) では filter 無効化 → 既存 test 挙動保存
        if df is not None and not df.empty and book_price_min > 0:
            from config.books import is_price_book_acceptable
            from collections import namedtuple as _nt
            current_price = float(df["close"].iloc[-1])
            _PricedBook = _nt("_PricedBook", ["price_min", "price_max"])
            _book_proxy = _PricedBook(price_min=book_price_min, price_max=book_price_max)
            if not is_price_book_acceptable(current_price, _book_proxy):
                try:
                    _record_with_evs(
                        ticker=ticker, decision="pass", today=today,
                        book=book, consensus=consensus, edge=edge,
                        council_reason=f"price_filter:{current_price:.0f}", db_path=db_path,
                        evs_comp=evs_comp,
                    )
                except Exception as e:
                    errors.append(f"decision_shadow:{ticker}:{e}")
                continue

        # A/B: treatment ブックは弱気レジーム(p4)時のみ建玉（regime_filter）。
        if regime_filter and not vote_result.get("p4"):
            try:
                _record_with_evs(
                    ticker=ticker, decision="pass", today=today,
                    book=book, consensus=consensus, edge=edge,
                    council_reason="regime_filter_skip:p4=False", db_path=db_path,
                    evs_comp=evs_comp,
                )
            except Exception as e:
                errors.append(f"decision_shadow:{ticker}:{e}")
            continue
        if ticker in existing_tickers:
            logger.info("Skip %s: already has open position", ticker)
            try:
                _record_with_evs(
                    ticker=ticker, decision="pass", today=today,
                    book=book, consensus=consensus, edge=edge,
                    council_reason="already_open", db_path=db_path,
                    evs_comp=evs_comp,
                )
            except Exception as e:
                errors.append(f"decision_shadow:{ticker}:{e}")
            continue

        if df is None or df.empty:
            try:
                _record_with_evs(
                    ticker=ticker, decision="pass", today=today,
                    book=book, consensus=consensus, edge=edge,
                    council_reason="no_market_data", db_path=db_path,
                    evs_comp=evs_comp,
                )
            except Exception as e:
                errors.append(f"decision_shadow:{ticker}:{e}")
            continue

        price = float(df["close"].iloc[-1])

        # v2 (Phase 0+): EVS あり + router_con あり → PLT lookup based sizing
        # + Portfolio-aware 3層制約 (2026-05-25): slot/cash/single_ticker
        sizing_decision: SizingDecision | None = None
        if evs_comp is not None and router_con is not None:
            try:
                from src.sizing.plt import assign_rsi_bin  # noqa: F401  (used via decide_cap)
                from src.sizing.router import compute_portfolio_aware_shares
                rsi_for_bin = _extract_indicators(df).get("rsi") or 20.0
                sizing_decision = decide_cap(
                    con=router_con, components=evs_comp,
                    consensus=consensus, rsi=float(rsi_for_bin),
                    nikkei_ma25_dev=nikkei_ma25_dev,
                    ticker=ticker, decided_date=today,
                )
                # Phase A Boyd cushion (2026-05-27): cap_pct を cushion 倍 (DD連動smooth縮小)
                # cushion=1.0 (HWM超え) → 変化なし、cushion=0.33 (DD-5%) → 33%にsize縮小
                if boyd_cushion < 1.0:
                    cushioned_cap = sizing_decision.cap_pct * boyd_cushion
                    sizing_decision = sizing_decision._replace(cap_pct=cushioned_cap) if hasattr(sizing_decision, '_replace') else type(sizing_decision)(
                        cap_pct=cushioned_cap,
                        source=sizing_decision.source,
                        cell_id=sizing_decision.cell_id,
                        cell_n_samples=sizing_decision.cell_n_samples,
                        cell_confidence=sizing_decision.cell_confidence,
                        exploration_flag=sizing_decision.exploration_flag,
                        fallback_used=sizing_decision.fallback_used,
                    )
                # 残スロット = max_concurrent - (open_count + 今回の position_size 既決定数)
                slots_remaining_now = max(slots_left - len(position_size), 1)
                shares, sizing_reason, effective_cap = compute_portfolio_aware_shares(
                    decision=sizing_decision,
                    capital=equity,
                    free_cash=remaining_cash,
                    price=price,
                    flex=flex,
                    slots_left=slots_remaining_now,
                )
                # 3層 cap適用 diagnostic log (2026-05-25)
                # forward paper で「どのlayerがcapを抑制したか」を追跡
                if sizing_reason in ("slot_limited", "ticker_ceiling", "cash_limited"):
                    logger.info(
                        "[%s] %s sizing limited by %s: raw_cap=%.3f → eff_cap=%.3f (free_cash=%.0f, slots_left=%d)",
                        book or "legacy", ticker, sizing_reason,
                        sizing_decision.cap_pct, effective_cap,
                        remaining_cash, slots_remaining_now,
                    )
            except Exception as e:
                errors.append(f"router:{ticker}:{e}")
                # Fallback to legacy kelly_size
                shares = kelly_size(consensus, equity, price, flex=flex)
        else:
            # EVS 計算失敗 or router_con なし → legacy kelly_size に fallback
            shares = kelly_size(consensus, equity, price, flex=flex)

        shares = _cap_shares_by_cash(shares, price, remaining_cash)
        if shares > 0:
            position_size[ticker] = shares
            remaining_cash -= shares * price + calc_commission(price, shares)
            running_held_tickers.add(ticker)  # F9 累積 (2026-05-25 fix)
            try:
                _record_with_evs(
                    ticker=ticker, decision="go", today=today,
                    book=book, consensus=consensus, edge=edge,
                    proposed_yen=shares * price,
                    council_reason="kelly_ok", db_path=db_path,
                    evs_comp=evs_comp, sizing_decision=sizing_decision,
                )
            except Exception as e:
                errors.append(f"decision_shadow:{ticker}:{e}")
        else:
            try:
                _record_with_evs(
                    ticker=ticker, decision="pass", today=today,
                    book=book, consensus=consensus, edge=edge,
                    council_reason="shares_zero", db_path=db_path,
                    evs_comp=evs_comp, sizing_decision=sizing_decision,
                )
            except Exception as e:
                errors.append(f"decision_shadow:{ticker}:{e}")

    if router_con is not None:
        try:
            router_con.close()
        except Exception:
            pass
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
    # Layer 9 cross-book dedup 削除済 (2026-05-27 Grove 認識訂正後):
    # - Paper: 5 book 完全独立 A/B 並列、各 book が同 signal を取って良い
    #   (Grove vision 「5船は奪い合うでなく独立並列実験」)
    # - LIVE: auCarbon 1 account = physical制約で自然 dedup、code 不要
    # - kabu commission ¥0 で commission節約 rationale 消滅
    # routing_priority sort も不要、BOOKS の宣言順で処理
    for bk in BOOKS:
        try:
            con = get_connection(db_path)
            equity, free_cash, open_tk = book_account(con, bk.book_id, bk.initial_capital)
            # Phase A Boyd cushion (2026-05-27): HWM 更新 + cushion 計算
            from src.data.db import update_book_hwm, compute_boyd_cushion
            from config.strategy_params import (
                DD_HARD_LIMIT, CUSHION_ALPHA, CUSHION_PAUSE_THRESHOLD,
            )
            hwm = update_book_hwm(con, bk.book_id, equity)  # HWM ratchet
            cushion = compute_boyd_cushion(
                equity=equity, hwm=hwm,
                dd_hard_limit=DD_HARD_LIMIT, alpha=CUSHION_ALPHA,
            )
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

        # Phase A: cushion < threshold で book 全 signal skip (dd_pause)
        if cushion < CUSHION_PAUSE_THRESHOLD:
            dd_pct = (equity - hwm) / hwm * 100 if hwm > 0 else 0.0
            logger.warning(
                "[%s] dd_pause: equity=¥%.0f HWM=¥%.0f DD=%.2f%% cushion=%.3f",
                bk.book_id, equity, hwm, dd_pct, cushion,
            )
            # decision_shadow に「pass with council_reason=dd_pause」記録は kelly_node で
            # しない (book 全停止なので skip)。Q4 で kill_criterion に dd_pause threshold追加。
            continue

        bstate: ScanState = {
            **s,
            "broker": MockTachibanaClient(initial_balance=free_cash),
            "book": bk.book_id,
            "book_equity": equity,
            "book_free_cash": free_cash,
            "book_flex": bk.flex,
            "book_regime_filter": bk.regime_filter,
            "book_max_concurrent": bk.max_concurrent,
            "book_open_tickers": open_tk,
            # 令和式 per-book playbook (Layer 7/8, 2026-05-25)
            "book_price_min": bk.price_min,
            "book_price_max": bk.price_max,
            "book_consensus_min_override": bk.consensus_min_override,
            # Phase A Boyd cushion (2026-05-27): sizing_multiplier として kelly_node で適用
            "book_boyd_cushion": cushion,
        }
        ks = kelly_node(bstate)
        bstate.update(ks)
        ex = execution_node(bstate)
        all_errors += ex.get("errors", [])

        pos = ks.get("position_size", {})
        logger.info("[%s] equity=%.0f free=%.0f flex=%s cushion=%.3f 注文%d件",
                    bk.book_id, equity, free_cash, bk.flex, cushion, len(pos))

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
