"""EDINET履歴開示キャッシュ。
Phase 1-B: 過去3年の全上場企業開示メタデータをDuckDBに蓄積。
文書本文取得+Qwen3分類は universe 内で docTypeCode フィルタ後のみ実行。
"""
from __future__ import annotations

import logging
import os
import time
from datetime import date, timedelta
from pathlib import Path

import duckdb
import requests

from src.data.edinet import EDINET_API_BASE, list_filings, filter_listed, normalize_ticker
from src.data.edinet_doc import classify_filing

logger = logging.getLogger("data.edinet_cache")

CACHE_DB = Path(__file__).resolve().parent.parent.parent / "data" / "edinet_cache.duckdb"
# インパクト大候補 docTypeCode のみ分類対象 (定型の有報・四半期は除外)
IMPACT_CANDIDATE_CODES = {"180", "190"}  # 臨時報告書 + 訂正 (最インパクト大の文書型に絞り)

THROTTLE_LIST = 0.3       # documents.json list (軽い)
THROTTLE_DOC = 0.6        # document body fetch (重い zip)


def init_cache():
    CACHE_DB.parent.mkdir(parents=True, exist_ok=True)
    con = duckdb.connect(str(CACHE_DB))
    con.execute("""
        CREATE TABLE IF NOT EXISTS filings (
            doc_id TEXT PRIMARY KEY,
            filing_date DATE NOT NULL,
            sec_code TEXT,
            ticker4 TEXT,
            filer_name TEXT,
            doc_type_code TEXT,
            doc_description TEXT
        );
    """)
    con.execute("CREATE INDEX IF NOT EXISTS idx_ticker_date ON filings(ticker4, filing_date)")
    con.execute("""
        CREATE TABLE IF NOT EXISTS classifications (
            doc_id TEXT PRIMARY KEY,
            impact TEXT,
            reason TEXT,
            body_len INTEGER,
            classified_at TIMESTAMP DEFAULT now()
        );
    """)
    con.execute("""
        CREATE TABLE IF NOT EXISTS fetch_progress (
            fetch_date DATE PRIMARY KEY,
            status TEXT,
            completed_at TIMESTAMP DEFAULT now()
        );
    """)
    con.close()


def bulk_fetch_metadata(start: date, end: date) -> dict:
    """期間内の全日について開示一覧を取得してDBに保存。"""
    init_cache()
    con = duckdb.connect(str(CACHE_DB))
    done = {r[0] for r in con.execute("SELECT fetch_date FROM fetch_progress WHERE status='ok'").fetchall()}
    con.close()

    stats = {"dates_ok": 0, "dates_err": 0, "filings_added": 0}
    d = start
    while d <= end:
        # skip weekends
        if d.weekday() >= 5 or d in done:
            d += timedelta(days=1)
            continue
        try:
            filings = list_filings(d)
            listed = filter_listed(filings)
            con = duckdb.connect(str(CACHE_DB))
            added = 0
            for f in listed:
                try:
                    con.execute("""
                        INSERT OR IGNORE INTO filings
                        (doc_id, filing_date, sec_code, ticker4, filer_name, doc_type_code, doc_description)
                        VALUES (?,?,?,?,?,?,?)
                    """, [f["docID"], d, f.get("secCode",""), normalize_ticker(f.get("secCode","")),
                          f.get("filerName","")[:100], f.get("docTypeCode",""),
                          f.get("docDescription","")[:300]])
                    added += 1
                except Exception:
                    pass
            con.execute("INSERT OR REPLACE INTO fetch_progress (fetch_date, status) VALUES (?, 'ok')", [d])
            con.close()
            stats["dates_ok"] += 1
            stats["filings_added"] += added
        except Exception as e:
            logger.warning("fetch fail %s: %s", d, str(e)[:80])
            stats["dates_err"] += 1
        time.sleep(THROTTLE_LIST)
        if stats["dates_ok"] % 20 == 0 and stats["dates_ok"] > 0:
            logger.info("list progress: %s ok=%d err=%d added=%d",
                        d, stats["dates_ok"], stats["dates_err"], stats["filings_added"])
        d += timedelta(days=1)
    return stats


def get_candidate_docs(universe_codes: list[str], start: date, end: date) -> list[dict]:
    """分類対象のdocsを取得。universe_codes (4桁) + docTypeCode フィルタ。"""
    init_cache()
    con = duckdb.connect(str(CACHE_DB), read_only=True)
    codes4 = [c[:4] for c in universe_codes]
    placeholders = ",".join(["?"] * len(codes4))
    types_placeholders = ",".join(["?"] * len(IMPACT_CANDIDATE_CODES))
    sql = f"""
        SELECT doc_id, filing_date, ticker4, filer_name, doc_type_code, doc_description
        FROM filings
        WHERE ticker4 IN ({placeholders})
          AND doc_type_code IN ({types_placeholders})
          AND filing_date BETWEEN ? AND ?
        ORDER BY filing_date
    """
    rows = con.execute(sql, codes4 + list(IMPACT_CANDIDATE_CODES) + [start, end]).fetchall()
    con.close()
    cols = ["docID", "filing_date", "ticker4", "filerName", "docTypeCode", "docDescription"]
    return [dict(zip(cols, r)) for r in rows]


def classify_batch(candidates: list[dict], skip_existing: bool = True,
                   workers: int = 6) -> dict:
    """候補文書をQwen3で分類してDBに保存。ThreadPoolExecutorでEDINET+Qwen並列化。"""
    from concurrent.futures import ThreadPoolExecutor, as_completed
    import threading

    init_cache()
    con = duckdb.connect(str(CACHE_DB))
    existing = set()
    if skip_existing:
        existing = {r[0] for r in con.execute("SELECT doc_id FROM classifications").fetchall()}
    con.close()

    todo = [c for c in candidates if c["docID"] not in existing]
    logger.info("classify_batch: %d to classify (skip %d existing), workers=%d",
                len(todo), len(candidates)-len(todo), workers)

    stats = {"ok": 0, "err": 0, "neg": 0, "pos": 0, "neu": 0}
    stats_lock = threading.Lock()
    db_lock = threading.Lock()

    def process_one(doc: dict) -> None:
        try:
            res = classify_filing(doc)
            s = res.get("sentiment_llm", {})
            impact = s.get("impact", "unknown")
            reason = s.get("reason", "")[:80]
            body_len = res.get("body_len", 0)
            with db_lock:
                con = duckdb.connect(str(CACHE_DB))
                con.execute("INSERT OR REPLACE INTO classifications (doc_id, impact, reason, body_len) VALUES (?,?,?,?)",
                            [doc["docID"], impact, reason, body_len])
                con.close()
            with stats_lock:
                stats["ok"] += 1
                if impact == "negative": stats["neg"] += 1
                elif impact == "positive": stats["pos"] += 1
                elif impact == "neutral": stats["neu"] += 1
        except Exception as e:
            with stats_lock:
                stats["err"] += 1
            logger.warning("classify fail %s: %s", doc["docID"], str(e)[:60])

    done_count = 0
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futures = [ex.submit(process_one, d) for d in todo]
        for fut in as_completed(futures):
            done_count += 1
            if done_count % 50 == 0:
                with stats_lock:
                    logger.info("classify progress: %d/%d ok=%d neg=%d pos=%d err=%d",
                                done_count, len(todo), stats["ok"], stats["neg"], stats["pos"], stats["err"])
    return stats


def load_negatives(universe_codes: list[str], start: date, end: date) -> "pd.DataFrame":
    """negative判定された開示を (ticker4, filing_date, reason) で取得。"""
    import pandas as pd
    init_cache()
    con = duckdb.connect(str(CACHE_DB), read_only=True)
    codes4 = [c[:4] for c in universe_codes]
    ph = ",".join(["?"] * len(codes4))
    df = con.execute(f"""
        SELECT f.ticker4, f.filing_date, f.doc_id, f.filer_name, f.doc_description, c.reason
        FROM filings f JOIN classifications c ON f.doc_id = c.doc_id
        WHERE c.impact='negative' AND f.ticker4 IN ({ph})
          AND f.filing_date BETWEEN ? AND ?
        ORDER BY f.ticker4, f.filing_date
    """, codes4 + [start, end]).fetchdf()
    con.close()
    return df


def cache_stats() -> dict:
    init_cache()
    con = duckdb.connect(str(CACHE_DB), read_only=True)
    d_filings = con.execute("SELECT COUNT(*) FROM filings").fetchone()[0]
    d_classified = con.execute("SELECT COUNT(*) FROM classifications").fetchone()[0]
    d_neg = con.execute("SELECT COUNT(*) FROM classifications WHERE impact='negative'").fetchone()[0]
    d_dates = con.execute("SELECT MIN(filing_date), MAX(filing_date), COUNT(DISTINCT filing_date) FROM filings").fetchone()
    con.close()
    return {"filings": d_filings, "classified": d_classified, "negative": d_neg,
            "date_min": str(d_dates[0]), "date_max": str(d_dates[1]), "unique_dates": d_dates[2]}
