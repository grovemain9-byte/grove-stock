"""Phase 1 Step 1.1 最小実証: 立花 demo login → sUrlEventWebSocket の存在確認のみ。

目的: 2ヶ月計画の地盤を1コマンドで検証する。
  ✓ login成功
  ✓ 返却fieldに sUrlEventWebSocket あり
  → 取れたら Phase 1 Step 1.2 (実WebSocket接続) へ進める

これ以外のAPI呼び出しは一切しない。read-only 確認、即logout。
"""
from __future__ import annotations
import json
import logging
import os
import sys
import time
from datetime import datetime
from pathlib import Path
from urllib.parse import quote

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
logger = logging.getLogger("poc")

# .env読み込み (load_dotenvが入っていれば)
try:
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).resolve().parent.parent / ".env")
except Exception:
    pass

USER_ID = os.environ.get("TACHIBANA_USER_ID", "")
PASSWORD = os.environ.get("TACHIBANA_PASSWORD", "")
ENV = os.environ.get("TACHIBANA_ENV", "demo")
BASE = {
    "demo": "https://demo-kabuka.e-shiten.jp/e_api_v4r8/",
    "production": "https://kabuka.e-shiten.jp/e_api_v4r8/",
}[ENV]


def main() -> int:
    if not USER_ID or not PASSWORD:
        logger.error("credentials missing in .env (TACHIBANA_USER_ID / TACHIBANA_PASSWORD)")
        return 2

    import urllib3
    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
    http = urllib3.PoolManager(timeout=urllib3.Timeout(connect=10, read=30))

    ts = datetime.now().strftime("%Y.%m.%d-%H:%M:%S.") + f"{datetime.now().microsecond // 1000:03d}"
    params = {
        "p_no": "1",
        "p_sd_date": ts,
        "sCLMID": "CLMAuthLoginRequest",
        "sUserId": quote(USER_ID, safe=""),
        "sPassword": quote(PASSWORD, safe=""),
        "sJsonOfmt": "5",
    }
    body = "{" + ",".join(f'"{k}":"{v}"' for k, v in params.items()) + "}"
    url = BASE.rstrip("/") + "/auth/?" + body
    logger.info("env=%s, login attempt...", ENV)
    resp = http.request("GET", url)
    if resp.status != 200:
        logger.error("HTTP %d", resp.status)
        return 3
    decoded = resp.data.decode("shift-jis", errors="ignore")
    obj = json.loads(decoded)

    err = str(obj.get("p_errno", "-1"))
    rcode = str(obj.get("sResultCode", "-1"))
    if err != "0" or rcode != "0":
        logger.error("login failed: %s / %s", obj.get("p_err"), obj.get("sResultText"))
        return 4

    logger.info("✓ login OK")
    # 全 sUrl* キーを列挙
    url_keys = sorted(k for k in obj.keys() if k.lower().startswith("surl"))
    logger.info("returned URL fields: %s", url_keys)
    for k in url_keys:
        v = obj[k]
        if v:
            logger.info("  %s = %s", k, v[:80] + ("..." if len(str(v)) > 80 else ""))

    has_ws = any("event" in k.lower() and "websocket" in k.lower() for k in url_keys) \
        or "sUrlEventWebSocket" in obj
    if has_ws:
        logger.info("✅ sUrlEventWebSocket 取得確認 → Phase 1 Step 1.2 進行可")
        result_code = 0
    else:
        logger.warning("⚠ sUrlEventWebSocket field NOT FOUND")
        logger.warning("   v4r7+のWebSocket対応はaccount単位の権限の可能性 → 立花サポート確認 or 別経路")
        result_code = 5

    # logout
    url_req = obj.get("sUrlRequest", "")
    if url_req:
        ts2 = datetime.now().strftime("%Y.%m.%d-%H:%M:%S.") + f"{datetime.now().microsecond // 1000:03d}"
        params2 = {"p_no": "2", "p_sd_date": ts2, "sCLMID": "CLMAuthLogoutRequest", "sJsonOfmt": "5"}
        body2 = "{" + ",".join(f'"{k}":"{v}"' for k, v in params2.items()) + "}"
        http.request("GET", url_req + "?" + body2)
        logger.info("logged out")

    # 結果を /tmp に保存（次セッションが拾える）
    Path("/tmp/poc_tachibana_ws_url.json").write_text(json.dumps({
        "ts": datetime.now().isoformat(),
        "env": ENV,
        "url_fields_present": url_keys,
        "has_ws_url": has_ws,
        "sample_value_for_ws": obj.get("sUrlEventWebSocket", obj.get("sUrlEvent", "")),
    }, ensure_ascii=False, indent=2))
    logger.info("saved: /tmp/poc_tachibana_ws_url.json")
    return result_code


if __name__ == "__main__":
    sys.exit(main())
