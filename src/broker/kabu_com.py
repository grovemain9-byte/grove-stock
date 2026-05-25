"""auカブコム証券 (Mitsubishi UFJ eSmart Securities) kabuステーション API クライアント + Mock.

2026-05-25 新規 — 令和式 BNF AGI fund の commission ¥0 移行候補.

設計判断:
- TachibanaClient と同じ BrokerBase interface を実装 (既存 main.py/monitor.py を変更不要に)
- kabuステーション GUI (Windows VM上) が裏で動いている前提
- localhost:18080 (本番) or :18081 (検証) を Mac側 Python から呼ぶ
- VM ネットワーク経由 (Parallels shared network なら 10.211.55.x、UTM なら bridged)

認証フロー (公式 yaml v1.5 準拠):
  1. POST /token  body={"APIPassword": "<api_password>"} → X-API-KEY 返却
  2. kabuステーション 早朝強制ログアウト → daily 8:55cron で token 再取得
  3. token はメモリ保持、別の token 発行で無効化される

人間クリック要件:
  - per-trade: **不要** (POST /sendorder は X-API-KEY ヘッダのみ)
  - daily: **不要** (token 自動再取得)
  - 初期設定: kabuステーション GUI で「APIシステム設定」→ API password 設定 (1回のみ)
  - 異常時: token無効 → 自動 retry → 失敗時は alert

公式参照: https://github.com/kabucom/kabusapi
spec: reference/kabu_STATION_API.yaml v1.5

TODO (口座開設後の本格実装):
  - POST /sendorder 完全実装 (現状はskeleton、req body フィールド網羅必要)
  - POST /cancelorder 実装
  - GET /positions, /orders, /wallet, /board 実装
  - PUSH (WebSocket) 対応 (real-time price → trailing stop精度向上)
  - PriceVolumeStrategy 注文 (TWAP等) 検討
"""
from __future__ import annotations

import logging
import os
import random
import threading
import time
from datetime import datetime
from typing import Any

import requests

from src.broker.tachibana import BrokerBase  # 共通 interface 再利用

logger = logging.getLogger("broker.kabu_com")

# --- 環境 ---
# kabuステーション API server (Windows VM内で起動)
ENVS = {
    "production": "http://localhost:18080/kabusapi",  # 本番口座
    "demo": "http://localhost:18081/kabusapi",         # 検証口座
}

# 流量制限 (公式仕様)
RATE_LIMIT_ORDER = 5    # 発注系 秒間5件
RATE_LIMIT_INFO = 10    # 情報系 秒間10件
MIN_INTERVAL_ORDER = 1.0 / RATE_LIMIT_ORDER

# 注文区分 (公式 yaml enum)
# Side: 1=売, 2=買
SIDE_BUY = "2"
SIDE_SELL = "1"

# CashMargin: 1=現物, 2=新規(信用), 3=返済(信用)
CASH_MARGIN_GENBUTSU = 1

# DelivType: 0=指定なし, 1=自動振替, 2=お預り金, 3=auマネーコネクト
DELIV_TYPE_AUTO = 2

# FundType: '  '(半角スペース2)=現物売の場合, 'AA'=信用代用, '11'=信用買付
FUND_TYPE_SPOT_SELL = "  "
FUND_TYPE_SPOT_BUY = "AA"

# AccountType: 2=一般, 4=特定, 12=法人
ACCOUNT_TYPE_TOKUTEI = 4
ACCOUNT_TYPE_HOJIN = 12

# FrontOrderType: 10=成行, 13=寄成(前場), 14=寄成(後場), 15=引成(前場), 16=引成(後場), 20=指値, ...
FRONT_ORDER_MARKET = 10

# Exchange: 1=東証, 3=名証, 5=福証, 6=札証
EXCHANGE_TSE = 1


# === KabuComClient ===

class KabuComClient(BrokerBase):
    """kabuステーション API REST クライアント (公式 v1.5 準拠).

    使用例:
        client = KabuComClient(env="demo")
        client.login()  # token取得
        result = client.buy("7203", 100)
        print(result)  # {"status": "ok", "executed_price": ..., "executed_at": ...}

    Daily運用:
        cron 8:55 で `python -c "from src.broker.kabu_com import KabuComClient; KabuComClient().login()"`
        → token を file に保存しておけば再起動後も使い回せる (実装は別task)

    法人口座対応:
        AccountType=12 を指定。だが kabuステーション API は公式に「個人向け」と明記
        されているため、法人で実利用可否は要 サポート問い合わせ。
    """

    TOKEN_FILE = ".kabu_token"  # daily token 保存用 (Phase 2で実装)

    def __init__(
        self,
        api_password: str | None = None,
        env: str | None = None,
        account_type: int = ACCOUNT_TYPE_TOKUTEI,
        timeout: float = 5.0,
    ):
        self._api_password = api_password or os.getenv("KABU_API_PASSWORD", "")
        self._env = env or os.getenv("KABU_ENV", "demo")
        if self._env not in ENVS:
            self._env = "demo"
        self._base_url = ENVS[self._env]
        self._account_type = account_type
        self._timeout = timeout

        # token / 状態
        self._token: str | None = None
        self._token_issued_at: datetime | None = None
        self._lock = threading.Lock()
        self._last_order_time = 0.0

    # --- 認証 ---

    def login(self) -> bool:
        """POST /token で X-API-KEY 取得.

        早朝強制ログアウト後の再取得もこれで対応 (cron 8:55推奨).
        """
        if not self._api_password:
            logger.error("Login failed: KABU_API_PASSWORD not set")
            return False
        try:
            resp = requests.post(
                f"{self._base_url}/token",
                json={"APIPassword": self._api_password},
                headers={"Content-Type": "application/json"},
                timeout=self._timeout,
            )
            if resp.status_code != 200:
                logger.error("Login failed: status=%d body=%s", resp.status_code, resp.text[:200])
                return False
            data = resp.json()
            self._token = data.get("Token")
            if not self._token:
                logger.error("Login failed: Token field missing in response")
                return False
            self._token_issued_at = datetime.now()
            logger.info("Login OK: env=%s token=...%s", self._env, self._token[-6:])
            return True
        except requests.RequestException as e:
            logger.error("Login exception: %s", e)
            return False

    def is_logged_in(self) -> bool:
        return self._token is not None

    def logout(self) -> None:
        """Logout は明示endpointなし。token を捨てるだけ.

        kabuステーション GUI をログアウトすると自動でtoken無効化される.
        """
        self._token = None
        self._token_issued_at = None

    # --- 取引API ---

    def buy(self, ticker: str, shares: int) -> dict:
        """現物成行買い (Side=2, CashMargin=1, FrontOrderType=10=成行)."""
        return self._place_order(ticker, shares, side=SIDE_BUY, fund_type=FUND_TYPE_SPOT_BUY)

    def sell(self, ticker: str, shares: int) -> dict:
        """現物成行売り (Side=1)."""
        return self._place_order(ticker, shares, side=SIDE_SELL, fund_type=FUND_TYPE_SPOT_SELL)

    def get_balance(self) -> float:
        """取引余力 (GET /wallet/cash) の StockAccountWallet を返す.

        Phase 1: 実装スタブ. token あれば 0.0、なければ raise.
        Phase 2: 完全実装 → unit test → forward paper で confirm.
        """
        if not self.is_logged_in():
            raise RuntimeError("Not logged in")
        try:
            resp = requests.get(
                f"{self._base_url}/wallet/cash",
                headers={"X-API-KEY": self._token or ""},
                timeout=self._timeout,
            )
            if resp.status_code != 200:
                logger.warning("get_balance failed: %d %s", resp.status_code, resp.text[:200])
                return 0.0
            data = resp.json()
            return float(data.get("StockAccountWallet", 0.0))
        except (requests.RequestException, ValueError) as e:
            logger.error("get_balance exception: %s", e)
            return 0.0

    # --- internals ---

    def _place_order(self, ticker: str, shares: int, side: str, fund_type: str) -> dict:
        """POST /sendorder 共通実装.

        TODO Phase 2:
          - 実 API response の "OrderId" を返す
          - status pollerで約定確認 → executed_price 取得
          - 部分約定/未約定の処理
          - 信用口座対応 (CashMargin=2, ClosePositionOrder等)
          - エラーリトライ (token無効 → 自動 re-login)
        """
        if not self.is_logged_in():
            return {"status": "error", "message": "Not logged in"}

        # rate limit (発注系 秒5件)
        with self._lock:
            elapsed = time.time() - self._last_order_time
            if elapsed < MIN_INTERVAL_ORDER:
                time.sleep(MIN_INTERVAL_ORDER - elapsed)
            self._last_order_time = time.time()

        body = {
            "Password": "",  # 取引パスワード (環境変数経由で別途設定推奨)
            "Symbol": ticker,
            "Exchange": EXCHANGE_TSE,
            "SecurityType": 1,  # 1=株式
            "Side": side,
            "CashMargin": CASH_MARGIN_GENBUTSU,
            "DelivType": DELIV_TYPE_AUTO,
            "FundType": fund_type,
            "AccountType": self._account_type,
            "Qty": int(shares),
            "FrontOrderType": FRONT_ORDER_MARKET,
            "Price": 0,  # 成行は 0
            "ExpireDay": 0,  # 0=当日
        }
        try:
            resp = requests.post(
                f"{self._base_url}/sendorder",
                json=body,
                headers={"X-API-KEY": self._token or "", "Content-Type": "application/json"},
                timeout=self._timeout,
            )
            if resp.status_code != 200:
                logger.warning("sendorder failed: %d %s", resp.status_code, resp.text[:200])
                return {"status": "error", "message": f"HTTP {resp.status_code}: {resp.text[:100]}"}
            data = resp.json()
            order_id = data.get("OrderId")
            if not order_id:
                return {"status": "error", "message": "OrderId missing"}
            # Phase 1 skeleton: 約定価格未取得 (status poller は Phase 2)
            return {
                "status": "ok",
                "order_id": order_id,
                "executed_price": None,  # Phase 2 で /orders から取得
                "executed_at": datetime.now().isoformat(),
            }
        except requests.RequestException as e:
            return {"status": "error", "message": f"request_exception: {e}"}


# === MockKabuComClient (テスト用) ===

class MockKabuComClient(BrokerBase):
    """kabuステーション API モック (MockTachibanaClient とほぼ同形).

    現物 commission ¥0 (SOR想定) を反映 → forward paper で立花の22bps削減効果検証用.
    """

    def __init__(self, initial_balance: float = 1_000_000.0, prices: dict[str, float] | None = None):
        self._balance = initial_balance
        self._prices = prices or {}
        self._positions: dict[str, list[dict]] = {}

    def set_price(self, ticker: str, price: float) -> None:
        self._prices[ticker] = price

    def buy(self, ticker: str, shares: int) -> dict:
        base_price = self._prices.get(ticker, 1000.0)
        # 微スリッページ (実 API は SOR で best price 想定、若干上振れ)
        executed_price = round(base_price * random.uniform(0.998, 1.002), 1)
        cost = executed_price * shares
        # kabu は現物 commission ¥0 (SOR条件) - 立花の22bpsから完全消滅
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
        executed_price = round(base_price * random.uniform(0.998, 1.002), 1)
        proceeds = executed_price * shares
        # 売却も commission ¥0
        self._balance += proceeds
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
