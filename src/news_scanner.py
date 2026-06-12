"""Phase 1 L-A: news_scanner.
open positions の銘柄について EDINET 開示を取得 → Qwen3 分類 → DB保存。
monitor.py から呼び出され、negative判定時は news_negative exit を返す。
"""
from __future__ import annotations

import logging
from datetime import date, datetime
from typing import Optional

from src.data.db import get_connection
from src.data.edinet import list_filings, match_positions
from src.data.edinet_doc import classify_filing

logger = logging.getLogger("news_scanner")


def scan_and_store(open_tickers: list[str], target_date: Optional[date] = None,
                   db_path: str | None = None) -> list[dict]:
    """open銘柄の当日開示を取得→分類→DBに記録。
    Returns: negative判定された開示リスト (exit候補)。
    """
    if not open_tickers:
        return []
    target_date = target_date or date.today()
    try:
        filings = list_filings(target_date)
    except Exception as e:
        logger.error("EDINET list_filings failed: %s", e)
        return []
    matched = match_positions(filings, open_tickers)
    if not matched:
        logger.info("no matched filings for %d open tickers on %s", len(open_tickers), target_date)
        return []
    logger.info("matched filings: %d", len(matched))

    con = get_connection(db_path)
    negatives = []
    for f in matched:
        # Dedup: 既に分類済ならスキップ
        existing = con.execute(
            "SELECT impact FROM news_events WHERE ticker=? AND doc_id=?",
            [f["ticker4"], f["docID"]]).fetchone()
        if existing:
            if existing[0] == "negative":
                negatives.append({**f, "impact": "negative", "reason": "cached"})
            continue
        # 分類 (重い、1-3秒 per doc)
        try:
            classified = classify_filing(f)
        except Exception as e:
            logger.warning("classify_filing failed %s: %s", f["docID"], e)
            classified = {**f, "sentiment_llm": {"impact": "error", "reason": str(e)[:50]}, "body_len": 0}
        s = classified.get("sentiment_llm", {})
        impact = s.get("impact", "unknown")
        reason = s.get("reason", "")
        con.execute("""
            INSERT INTO news_events (scanned_at, ticker, doc_id, doc_type_code,
                                     filer_name, doc_description, impact, reason, body_len)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT (ticker, doc_id) DO UPDATE SET impact=excluded.impact, reason=excluded.reason
        """, [datetime.now(), f["ticker4"], f["docID"], f.get("docTypeCode",""),
              f.get("filerName","")[:100], f.get("docDescription","")[:200],
              impact, reason, classified.get("body_len", 0)])
        if impact == "negative":
            negatives.append({**f, "impact": impact, "reason": reason})
            logger.warning("NEGATIVE disclosure: %s %s — %s",
                           f["ticker4"], f.get("filerName",""), reason)
    con.close()
    return negatives


def mark_applied(ticker: str, doc_id: str, db_path: str | None = None):
    """news_negative exit が発動した事を記録。"""
    con = get_connection(db_path)
    con.execute("UPDATE news_events SET applied=TRUE WHERE ticker=? AND doc_id=?", [ticker, doc_id])
    con.close()


def pending_negatives(open_tickers: list[str], db_path: str | None = None) -> list[tuple[str, str, str]]:
    """未適用の negative判定を取得 (ticker, doc_id, reason)。monitor.py から呼ぶ。"""
    if not open_tickers:
        return []
    con = get_connection(db_path)
    placeholders = ",".join(["?"] * len(open_tickers))
    rows = con.execute(f"""
        SELECT ticker, doc_id, reason FROM news_events
        WHERE impact='negative' AND applied=FALSE AND ticker IN ({placeholders})
        ORDER BY scanned_at DESC
    """, [t[:4] for t in open_tickers]).fetchall()
    con.close()
    return rows
