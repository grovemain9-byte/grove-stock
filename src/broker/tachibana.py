"""立花証券e支店 APIクライアント + Mock。

Issue #2: execution_node / exit_node 共通インターフェース。
公式サンプル（e-shiten-jp GitHub, API v4r8）準拠。

認証フロー:
  1. 認証URL + auth/ に CLMAuthLoginRequest を送信
  2. 成功時、仮想URL（REQUEST/PRICE等）が返却
  3. 以降は仮想URLにリクエスト

制約:
  - 秒間10リクエスト上限
  - レスポンスはShift-JIS
  - 利用可能時間: 06:00-23:55
"""
from __future__ import annotations

import json
import logging
import os
import random
import threading
import time
from abc import ABC, abstractmethod
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import quote

logger = logging.getLogger("broker.tachibana")

# --- 環境URL ---
URLS = {
    "production": "https://kabuka.e-shiten.jp/e_api_v4r8/",
    "demo": "https://demo-kabuka.e-shiten.jp/e_api_v4r8/",
}

# --- CLMID ---
CLMID_LOGIN = "CLMAuthLoginRequest"
CLMID_LOGOUT = "CLMAuthLogoutRequest"
CLMID_NEW_ORDER = "CLMKabuNewOrder"
CLMID_PRICE = "CLMMfdsGetMarketPrice"
CLMID_BALANCE = "CLMZanKaiKanougaku"

# 売買区分
BAIBAI_BUY = "3"
BAIBAI_SELL = "1"

# 現物
GENKIN_SHINYOU_GENBUTSU = "0"
MARKET_TSE = "00"

# レートリミット
MAX_RPS = 10
MIN_INTERVAL = 1.0 / MAX_RPS


# === 共通インターフェース ===

class BrokerBase(ABC):
    """TachibanaClient と MockTachibanaClient の共通インターフェース。"""

    @abstractmethod
    def buy(self, ticker: str, shares: int) -> dict:
        """現物成行買い。戻り値: {"status": str, "executed_price": float, "executed_at": str}"""
        ...

    @abstractmethod
    def sell(self, ticker: str, shares: int) -> dict:
        """現物成行売り。戻り値: 同上"""
        ...

    @abstractmethod
    def get_balance(self) -> float:
        """現金残高を返す。"""
        ...


# === TachibanaClient ===

class TachibanaClient(BrokerBase):
    """立花証券e支店 APIクライアント。"""

    def __init__(
        self,
        user_id: str | None = None,
        password: str | None = None,
        second_password: str | None = None,
        env: str | None = None,
    ):
        self._user_id = user_id or os.getenv("TACHIBANA_USER_ID", "")
        self._password = password or os.getenv("TACHIBANA_PASSWORD", "")
        self._second_password = second_password or os.getenv("TACHIBANA_SECOND_PASSWORD", "")
        self._env = env or os.getenv("TACHIBANA_ENV", "demo")
        if self._env not in URLS:
            self._env = "demo"
        self._base_url = URLS[self._env]

        # 本番環境は第2パスワード必須
        if self._env == "production" and not self._second_password:
            raise ValueError("TACHIBANA_SECOND_PASSWORD is required for production")

        self._url_request = ""
        self._url_price = ""
        self._p_no = 0
        self._lock = threading.Lock()
        self._last_request_time = 0.0
        self._logged_in = False
        self._json_ofmt = "5"

    # --- 公開API ---

    def login(self) -> bool:
        """ログイン。成功時True。"""
        if not self._user_id or not self._password:
            logger.error("Login failed: credentials empty")
            return False

        try:
            self._p_no = 0
            params = {
                "p_no": str(self._next_p_no()),
                "p_sd_date": self._format_p_sd_date(),
                "sCLMID": CLMID_LOGIN,
                "sUserId": self._url_encode(self._user_id),
                "sPassword": self._url_encode(self._password),
                "sJsonOfmt": self._json_ofmt,
            }
            resp = self._send_request(self._base_url, params, is_auth=True)

            if str(resp.get("p_errno", "-1")) != "0" or str(resp.get("sResultCode", "-1")) != "0":
                logger.error("Login failed: %s", resp.get("sResultText", resp.get("p_err", "")))
                return False

            self._url_request = resp.get("sUrlRequest", "")
            self._url_price = resp.get("sUrlPrice", "")

            if not self._url_request:
                logger.error("Login failed: virtual URLs empty")
                return False

            self._logged_in = True
            logger.info("Login OK: env=%s", self._env)
            return True

        except Exception as e:
            logger.error("Login exception: %s", e)
            return False

    def logout(self) -> None:
        """ログアウト。"""
        if not self._logged_in:
            return
        try:
            params = {
                "p_no": str(self._next_p_no()),
                "p_sd_date": self._format_p_sd_date(),
                "sCLMID": CLMID_LOGOUT,
                "sJsonOfmt": self._json_ofmt,
            }
            self._send_request(self._url_request, params)
        except Exception as e:
            logger.warning("Logout error: %s", e)
        finally:
            self._logged_in = False
            self._url_request = ""
            self._url_price = ""

    def is_logged_in(self) -> bool:
        return self._logged_in

    def buy(self, ticker: str, shares: int) -> dict:
        """現物成行買い。"""
        return self._place_order(ticker, shares, BAIBAI_BUY)

    def sell(self, ticker: str, shares: int) -> dict:
        """現物成行売り。"""
        return self._place_order(ticker, shares, BAIBAI_SELL)

    def get_balance(self) -> float:
        """現金残高を取得。"""
        if not self._logged_in:
            raise RuntimeError("Not logged in")

        params = {
            "p_no": str(self._next_p_no()),
            "p_sd_date": self._format_p_sd_date(),
            "sCLMID": CLMID_BALANCE,
            "sJsonOfmt": self._json_ofmt,
        }
        resp = self._send_request(self._url_request, params)

        if str(resp.get("p_errno", "0")) != "0":
            raise RuntimeError(f"Balance query failed: {resp.get('p_err', '')}")

        try:
            return float(resp.get("sKaiKanougaku", "0"))
        except (ValueError, TypeError):
            return 0.0

    def get_price(self, ticker: str) -> float | None:
        """現在値を取得。"""
        if not self._logged_in:
            return None

        params = {
            "p_no": str(self._next_p_no()),
            "p_sd_date": self._format_p_sd_date(),
            "sCLMID": CLMID_PRICE,
            "sIssueCode": ticker,
            "sSizyouC": MARKET_TSE,
            "sJsonOfmt": self._json_ofmt,
            "aAttrList": [{"sIssueCode": ticker, "sSizyouC": MARKET_TSE}],
        }
        resp = self._send_request(self._url_price, params)

        try:
            price_data = resp.get("aMarketData", [{}])
            if isinstance(price_data, list) and price_data:
                return float(price_data[0].get("pDPP", 0))
        except (ValueError, TypeError, IndexError):
            pass
        return None

    # --- 内部メソッド ---

    def _place_order(self, ticker: str, shares: int, baibai: str) -> dict:
        """注文送信。"""
        if not self._logged_in:
            return {"status": "error", "message": "Not logged in"}

        if not self._second_password and self._env == "production":
            return {"status": "error", "message": "Second password required"}

        params = {
            "p_no": str(self._next_p_no()),
            "p_sd_date": self._format_p_sd_date(),
            "sCLMID": CLMID_NEW_ORDER,
            "sGenkinShinyouKubun": GENKIN_SHINYOU_GENBUTSU,
            "sIssueCode": ticker,
            "sSizyouC": MARKET_TSE,
            "sBaibaiKubun": baibai,
            "sCondition": "0",
            "sOrderPrice": "*",  # 成行
            "sOrderSuryou": str(shares),
            "sZyoutoekiKazeiC": "1",  # 特定口座
            "sOrderExpireDay": "0",
            "sGyakusasiOrderType": "0",
            "sGyakusasiZyouken": "0",
            "sGyakusasiPrice": "*",
            "sTatebiType": "*",
            "sTategyokuZyoutoekiKazeiC": "*",
            "sSecondPassword": self._url_encode(self._second_password),
            "sJsonOfmt": self._json_ofmt,
        }

        resp = self._send_request(self._url_request, params)

        if str(resp.get("sResultCode", "-1")) != "0":
            return {
                "status": "error",
                "message": resp.get("sResultText", "Unknown error"),
            }

        side = "BUY" if baibai == BAIBAI_BUY else "SELL"
        logger.info("Order OK: %s %s x%d -> order_no=%s", side, ticker, shares,
                     resp.get("sOrderNumber", ""))

        return {
            "status": "ok",
            "executed_price": float(resp.get("sOrderPrice", "0") or "0"),
            "executed_at": datetime.now().isoformat(),
            "order_number": resp.get("sOrderNumber", ""),
        }

    def _next_p_no(self) -> int:
        with self._lock:
            self._p_no += 1
            return self._p_no

    def _format_p_sd_date(self) -> str:
        return datetime.now().strftime("%Y.%m.%d-%H:%M:%S.") + f"{datetime.now().microsecond // 1000:03d}"

    @staticmethod
    def _url_encode(value: str) -> str:
        return quote(value, safe="")

    def _wait_rate_limit(self) -> None:
        with self._lock:
            now = time.time()
            elapsed = now - self._last_request_time
            if elapsed < MIN_INTERVAL:
                time.sleep(MIN_INTERVAL - elapsed)
            self._last_request_time = time.time()

    def _build_params_json(self, params: dict[str, Any]) -> str:
        parts = []
        for key, value in params.items():
            if key.startswith("a"):
                parts.append(f'"{key}":{json.dumps(value, ensure_ascii=False)}')
            else:
                parts.append(f'"{key}":"{value}"')
        return "{" + ",".join(parts) + "}"

    def _send_request(self, url: str, params: dict[str, Any], *, is_auth: bool = False) -> dict:
        """APIリクエスト送信。"""
        self._wait_rate_limit()

        target_url = url
        if is_auth:
            target_url = target_url.rstrip("/") + "/auth/"

        json_str = self._build_params_json(params)
        full_url = f"{target_url}?{json_str}"

        import urllib3
        urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
        http = urllib3.PoolManager(
            timeout=urllib3.Timeout(connect=10.0, read=30.0),
            retries=urllib3.Retry(total=2, backoff_factor=0.5),
        )

        response = http.request("GET", full_url)

        if response.status != 200:
            return {"p_errno": str(response.status), "p_err": f"HTTP {response.status}"}

        decoded = response.data.decode("shift-jis", errors="ignore")
        return json.loads(decoded)


# === MockTachibanaClient ===

class MockTachibanaClient(BrokerBase):
    """テスト用Mock。buy/sellは現在値±0.5%のランダム約定価格を返す。"""

    def __init__(self, initial_balance: float = 1_000_000.0, prices: dict[str, float] | None = None):
        self._balance = initial_balance
        self._prices = prices or {}
        self._positions: dict[str, list[dict]] = {}

    def set_price(self, ticker: str, price: float) -> None:
        """テスト用に銘柄の現在値を設定。"""
        self._prices[ticker] = price

    def buy(self, ticker: str, shares: int) -> dict:
        base_price = self._prices.get(ticker, 1000.0)
        executed_price = round(base_price * random.uniform(0.995, 1.005), 1)
        cost = executed_price * shares

        if cost > self._balance:
            return {"status": "error", "message": "Insufficient balance"}

        self._balance -= cost

        if ticker not in self._positions:
            self._positions[ticker] = []
        self._positions[ticker].append({"shares": shares, "price": executed_price})

        return {
            "status": "ok",
            "executed_price": executed_price,
            "executed_at": datetime.now().isoformat(),
        }

    def sell(self, ticker: str, shares: int) -> dict:
        base_price = self._prices.get(ticker, 1000.0)
        executed_price = round(base_price * random.uniform(0.995, 1.005), 1)
        proceeds = executed_price * shares

        self._balance += proceeds

        # ポジションから削除
        if ticker in self._positions:
            remaining = shares
            new_positions = []
            for pos in self._positions[ticker]:
                if remaining <= 0:
                    new_positions.append(pos)
                elif pos["shares"] <= remaining:
                    remaining -= pos["shares"]
                else:
                    new_positions.append({"shares": pos["shares"] - remaining, "price": pos["price"]})
                    remaining = 0
            self._positions[ticker] = new_positions

        return {
            "status": "ok",
            "executed_price": executed_price,
            "executed_at": datetime.now().isoformat(),
        }

    def get_balance(self) -> float:
        return self._balance
