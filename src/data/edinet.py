"""EDINET 適時開示アダプタ (金融庁公式 API v2)。
Phase 1 L-A: open positions の銘柄について当日開示をスキャン。
"""
from __future__ import annotations

import logging
import os
import time
from datetime import date, datetime, timedelta
from pathlib import Path

import requests

logger = logging.getLogger("data.edinet")

EDINET_API_BASE = "https://disclosure.edinet-fsa.go.jp/api/v2"
DOC_DESC_SCAN_LIMIT = 10_000  # metadata fetch: 全開示取る, type=2

# 株価インパクト大のdocTypeCode (金融庁定義)
# 120=有価証券報告書, 130=訂正, 140=四半期報告書, 150=四半期訂正,
# 160=半期報告書, 170=半期訂正, 180=臨時報告書, 190=臨時訂正,
# 220=大量保有, 350=内部統制
NEG_KEYWORDS = ["下方修正", "特別損失", "減損", "業績予想の修正", "訴訟", "監査法人交代", "不適正意見", "希薄化", "減配", "MBO", "上場廃止"]
POS_KEYWORDS = ["上方修正", "自己株式取得", "自社株買い", "増配", "業務提携", "新規受注", "買収"]


def _key() -> str:
    k = os.getenv("EDINET_API_KEY", "").strip()
    if not k:
        raise RuntimeError("EDINET_API_KEY not set in env")
    return k


def list_filings(target_date: date) -> list[dict]:
    """指定日の全開示一覧。type=2で書類本文メタデータ取得。"""
    ds = target_date.strftime("%Y-%m-%d") if isinstance(target_date, date) else str(target_date)
    r = requests.get(f"{EDINET_API_BASE}/documents.json",
                     params={"date": ds, "type": "2", "Subscription-Key": _key()},
                     timeout=20)
    r.raise_for_status()
    j = r.json()
    if j.get("metadata", {}).get("status") != "200":
        raise RuntimeError(f"EDINET error: {j.get('metadata')}")
    return j.get("results", [])


def filter_listed(filings: list[dict]) -> list[dict]:
    """上場企業の開示のみ (secCode付き)。"""
    return [f for f in filings if f.get("secCode")]


def normalize_ticker(sec_code: str) -> str:
    """EDINETのsecCodeは5桁 (例 72030)。先頭4桁を返す (例 7203)."""
    return (sec_code or "")[:4]


def match_positions(filings: list[dict], open_codes: list[str]) -> list[dict]:
    """open positionsの銘柄コードに紐づく開示をフィルタ。
    open_codes: 4桁 or 5桁 どちらも受容、4桁でマッチ。
    """
    open_4 = {c[:4] for c in open_codes}
    matched = []
    for f in filter_listed(filings):
        t4 = normalize_ticker(f["secCode"])
        if t4 in open_4:
            f_copy = dict(f)
            f_copy["ticker4"] = t4
            matched.append(f_copy)
    return matched


def sentiment_by_keywords(filing: dict) -> str:
    """簡易ルールベース分類 (keyword match)。
    Qwen3分類器の前段フィルタ。確度が高いもの即決。
    Returns: 'neg', 'pos', 'unknown'
    """
    text = " ".join([filing.get("docDescription", ""), filing.get("docTypeCode", ""),
                     filing.get("docInfoEditStatus", "")])
    for kw in NEG_KEYWORDS:
        if kw in text:
            return "neg"
    for kw in POS_KEYWORDS:
        if kw in text:
            return "pos"
    return "unknown"


def fetch_today_disclosures(open_position_codes: list[str]) -> list[dict]:
    """本日の開示をスキャンし、open positions にマッチした開示を返す。
    各開示に 'sentiment' (keyword判定) を付与。
    """
    today = date.today()
    filings = list_filings(today)
    matched = match_positions(filings, open_position_codes)
    for f in matched:
        f["sentiment_keyword"] = sentiment_by_keywords(f)
    return matched
