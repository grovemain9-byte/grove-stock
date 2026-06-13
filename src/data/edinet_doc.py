"""EDINET文書本文取得 + クリーニング + Qwen3分類。"""
from __future__ import annotations

import io
import logging
import os
import re
import zipfile
from typing import Optional

import requests
from bs4 import BeautifulSoup

logger = logging.getLogger("data.edinet_doc")

EDINET_API_BASE = "https://disclosure.edinet-fsa.go.jp/api/v2"
OLLAMA_URL = os.getenv("OLLAMA_URL", "http://localhost:11434")
CLASSIFIER_MODEL = os.getenv("NEWS_CLASSIFIER_MODEL", "qwen3:30b-a3b")


def fetch_body_text(doc_id: str, max_chars: int = 6000) -> Optional[str]:
    """EDINETから文書ZIPを取得し、本文HTMLをテキスト抽出。"""
    key = os.getenv("EDINET_API_KEY", "").strip()
    if not key:
        raise RuntimeError("EDINET_API_KEY not set")
    r = requests.get(f"{EDINET_API_BASE}/documents/{doc_id}",
                     params={"type": "1", "Subscription-Key": key}, timeout=30)
    if r.status_code != 200:
        logger.warning("doc fetch failed %s: %d", doc_id, r.status_code)
        return None
    try:
        z = zipfile.ZipFile(io.BytesIO(r.content))
    except zipfile.BadZipFile:
        return None
    body_names = [n for n in z.namelist() if "honbun" in n and n.endswith(".htm")]
    if not body_names:
        body_names = [n for n in z.namelist() if n.endswith(".htm")]
    if not body_names:
        return None
    html = z.read(body_names[0]).decode("utf-8", errors="replace")
    soup = BeautifulSoup(html, "lxml")
    for tag in soup(["style", "script"]):
        tag.decompose()
    text = soup.get_text(separator="\n", strip=True)
    text = re.sub(r"\n{2,}", "\n", text)
    return text[:max_chars]


CLASSIFY_PROMPT = """以下は日本上場企業が提出した金融庁開示書類の本文です。
この開示が株価に与える影響を "negative" / "positive" / "neutral" のいずれかに分類し、
理由を40字以内で日本語で出力してください。

出力JSON形式厳守:
{{"impact": "negative|positive|neutral", "reason": "40字以内"}}

判断基準:
- negative: 下方修正, 特別損失, 減損, 訴訟, 監査法人交代, 減配, 業績悪化, 重大不祥事
- positive: 上方修正, 自社株買い, 増配, 業務提携, 新規受注, M&A買収側, 好決算
- neutral: 定型報告書 (四半期/半期の通常報告), 子会社移動でも業績影響小, 形式訂正

開示本文:
---
{body}
---

JSON:"""


def classify_with_qwen(body: str, timeout: int = 60) -> dict:
    """Qwen3 30B-A3Bで開示を分類。Returns {'impact': ..., 'reason': ...}."""
    import json
    prompt = CLASSIFY_PROMPT.format(body=body[:4000])
    r = requests.post(f"{OLLAMA_URL}/api/generate", json={
        "model": CLASSIFIER_MODEL,
        "prompt": prompt,
        "stream": False,
        "think": False,
        "format": {"type": "object",
                   "properties": {"impact": {"type": "string", "enum": ["negative","positive","neutral"]},
                                  "reason": {"type": "string"}},
                   "required": ["impact","reason"]},
        "options": {"temperature": 0.2, "num_predict": 150},
    }, timeout=timeout)
    if r.status_code != 200:
        return {"impact": "error", "reason": f"http {r.status_code}"}
    try:
        resp = r.json().get("response", "")
        m = re.search(r"\{.*\}", resp, re.DOTALL)
        if not m:
            return {"impact": "error", "reason": "no json"}
        parsed = json.loads(m.group(0))
        return {"impact": parsed.get("impact", "unknown"),
                "reason": str(parsed.get("reason", ""))[:80]}
    except Exception as e:
        return {"impact": "error", "reason": str(e)[:60]}


def classify_filing(filing: dict) -> dict:
    """filing (EDINET metadata) に sentiment_llm を付与して返す。"""
    result = dict(filing)
    body = fetch_body_text(filing["docID"])
    if not body:
        result["sentiment_llm"] = {"impact": "error", "reason": "no body"}
        return result
    result["body_len"] = len(body)
    result["sentiment_llm"] = classify_with_qwen(body)
    return result
