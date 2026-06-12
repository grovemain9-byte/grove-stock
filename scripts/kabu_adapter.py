"""kabu_adapter.py — kabu STATION API (kabusapi) thin scaffold for near-close MOC.

==============================================================================
  *** UNTESTED-UNTIL-ACCOUNT ***
  Grove の au カブコム証券口座は現在開設中 (mid-setup)。
  kabuステーション GUI (Windows VM) も未稼働のため、本ファイルは **今夜は疎通検証不能**。
  → spec-faithful に書き、全 API 呼び出しを try/except で graceful-degradation し、
    検証ポートは 18081 (検証/sandbox) のみを向ける。
  → **実発注パスは無効化** (ARM_REAL_ORDERS=False)。/sendorder は dry-run dict を返すだけ。
    口座稼働後に: ①ARM を外す前に unit/疎通 test ②forward paper で約定確認 ③Grove 承認。
==============================================================================

何のためのファイルか:
  intraday_meanrev.py (純粋な判断ロジック) が出した「引け際に引成(後場)で N株買う」
  という決定を、kabuステーション API に渡す **薄い API 層**。判断は一切しない。

既存 src/broker/kabu_com.py との関係:
  - kabu_com.py は本番 BrokerBase 実装 (FrontOrderType=10=成行、port 18080/18081)。
    本タスクは ADDITIVE-ONLY のため kabu_com.py は **変更しない**。
  - 本 adapter は「引け際 MOC 戦略専用」の独立 scaffold:
      * FrontOrderType=16 (引成・後場 = Market-On-Close) を使う ← 戦略の核
      * WebSocket PUSH (CurrentPrice) consumer を持つ (near-close monitor 用)
      * GET /board fallback を持つ (PUSH 切断時)
    口座稼働後、kabu_com.py に統合するか本 adapter を昇格させるかは別途判断。

公式参照:
  - https://github.com/kabucom/kabusapi  (REST yaml v1.5)
  - reference/kabu_STATION_API.yaml (repo 同梱の spec)

認証フロー (公式 v1.5):
  1. POST /token  body={"APIPassword": "<pw>"} → {"Token": "..."} (X-API-KEY として使う)
  2. 早朝強制ログアウト → daily 8:55 cron で再取得
  3. token はメモリ保持。別 token 発行で無効化。

エンドポイント (本 scaffold が触れる範囲):
  POST /token              認証
  PUT  /register           PUSH 配信銘柄登録 (<=50 銘柄)
  WS   /websocket          PUSH 受信 (CurrentPrice 等)
  GET  /board/{symbol@exch} 時価 (PUSH fallback)
  POST /sendorder          発注 (FrontOrderType=16 引成後場) ← ARM 無効で dry-run

TODO-VERIFY (口座稼働後に実値で確認):
  - [ ] 2026 取引所コード改定: Exchange=9 (SOR) / 27 など新コードの実際の番号。
        現状 EXCHANGE_TSE=1 を使い、SOR は EXCHANGE_SOR_2026=9 を仮置き (要確認)。
  - [ ] 引成後場 FrontOrderType=16 が現物(CashMargin=1)で受理されるか
  - [ ] 法人口座 (AccountType=12) で API 実利用可否 (公式は個人向け明記)
  - [ ] /sendorder の Password(取引パスワード)要否と渡し方
  - [ ] PUSH の実フィールド名 (CurrentPrice / CurrentPriceTime のキャメル正確性)
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable

logger = logging.getLogger("kabu_adapter")

# ===========================================================================
# 安全フラグ — 実発注は明示 ARM するまで無効 (NO real order path)
# ===========================================================================
# ハードゲート: True にしても、さらに口座稼働 + Grove 承認 + verify が前提。
# 今夜は必ず False。/sendorder は dry-run dict を返すだけ。
ARM_REAL_ORDERS: bool = False

# ===========================================================================
# 環境 — 検証ポート 18081 のみを既定にする (sandbox)
# ===========================================================================
ENVS = {
    "demo": "http://localhost:18081/kabusapi",        # 検証口座 (本タスクのターゲット)
    "production": "http://localhost:18080/kabusapi",   # 本番 (今夜は使わない)
}
WS_ENVS = {
    "demo": "ws://localhost:18081/kabusapi/websocket",
    "production": "ws://localhost:18080/kabusapi/websocket",
}

# --- 流量制限 (公式) ---
RATE_LIMIT_ORDER = 5          # 発注系 秒5件
RATE_LIMIT_INFO = 10          # 情報系 秒10件
MIN_INTERVAL_ORDER = 1.0 / RATE_LIMIT_ORDER
REGISTER_MAX_SYMBOLS = 50     # PUT /register は最大50銘柄

# --- 注文 enum (公式 yaml v1.5) ---
SIDE_SELL = "1"
SIDE_BUY = "2"
CASH_MARGIN_GENBUTSU = 1      # 1=現物
DELIV_TYPE_AUTO = 2           # 2=お預り金
FUND_TYPE_SPOT_BUY = "AA"     # 現物買付
FUND_TYPE_SPOT_SELL = "  "    # 現物売 (半角スペース2)
ACCOUNT_TYPE_TOKUTEI = 4      # 4=特定
ACCOUNT_TYPE_HOJIN = 12       # 12=法人 (要確認)
SECURITY_TYPE_STOCK = 1       # 1=株式

# --- FrontOrderType (公式 enum) — 本戦略の核は 16 ---
FRONT_MARKET = 10             # 成行
FRONT_OPEN_AM = 13            # 寄成(前場)
FRONT_OPEN_PM = 14            # 寄成(後場)
FRONT_CLOSE_AM = 15           # 引成(前場)
FRONT_CLOSE_PM = 16           # 引成(後場) = Market-On-Close ★ 引け際戦略はこれ
FRONT_LIMIT = 20             # 指値

# --- Exchange (公式 + 2026改定 TODO-VERIFY) ---
EXCHANGE_TSE = 1              # 東証
EXCHANGE_NSE = 3             # 名証
# TODO-VERIFY: 2026 取引所コード改定。SOR ルーティング/新コードの実番号は要確認。
#   下記は仮置き (公式 changelog で 9=SOR・27=? の表記を確認するまで信用しない)。
EXCHANGE_SOR_2026 = 9        # 仮: SOR (Smart Order Routing)  ← 要確認
EXCHANGE_CODE_27_2026 = 27   # 仮: 2026改定で言及される 27 番  ← 要確認


# ===========================================================================
# 戻り値型
# ===========================================================================

@dataclass(frozen=True)
class ApiResult:
    """全 API 呼び出しの統一戻り値。graceful-degradation 用。

    ok=False のとき degraded=True なら「fallback すべき」を意味する。
    """
    ok: bool
    data: dict[str, Any] = field(default_factory=dict)
    error: str = ""
    degraded: bool = False     # True: 上位は fallback (board/cache) に切替えるべき
    status_code: int | None = None


# ===========================================================================
# KabuMocAdapter — 引け際 MOC 専用 scaffold
# ===========================================================================

class KabuMocAdapter:
    """kabuステーション API thin adapter (引成後場 MOC 戦略専用).

    *** UNTESTED-UNTIL-ACCOUNT ***
    口座稼働まで疎通不能。全メソッドは try/except で ApiResult を返し、例外は
    握って degraded=True にする (graceful-degradation。落ちない)。

    使用例 (口座稼働後):
        ad = KabuMocAdapter(env="demo")
        if ad.login().ok:
            ad.register(["7203", "6758"])
            ad.start_push(on_price=lambda p: print(p))   # PUSH consumer
            # 引け5分前に intraday_meanrev が決めた発注:
            ad.send_moc_order("7203", shares=300, side=SIDE_BUY)  # ARM=False なら dry-run
    """

    def __init__(
        self,
        api_password: str | None = None,
        env: str = "demo",                 # 既定 demo = 検証ポート 18081
        account_type: int = ACCOUNT_TYPE_TOKUTEI,
        timeout: float = 5.0,
        exchange: int = EXCHANGE_TSE,
    ):
        self._api_password = api_password or os.getenv("KABU_API_PASSWORD", "")
        self._env = env if env in ENVS else "demo"
        self._base_url = ENVS[self._env]
        self._ws_url = WS_ENVS[self._env]
        self._account_type = account_type
        self._timeout = timeout
        self._exchange = exchange

        self._token: str | None = None
        self._token_issued_at: datetime | None = None
        self._registered: list[str] = []
        self._lock = threading.Lock()
        self._last_order_time = 0.0

        # PUSH consumer 状態
        self._ws_thread: threading.Thread | None = None
        self._ws_stop = threading.Event()
        self._last_prices: dict[str, float] = {}

    # ----- 共通 HTTP (requests を遅延 import、未導入でも壊さない) -----

    def _requests(self):
        """requests を遅延 import。未導入なら None を返し degraded 扱いにする。"""
        try:
            import requests  # noqa: PLC0415  (遅延 import は graceful-degradation の意図)
            return requests
        except ImportError:
            logger.error("requests not installed — adapter degraded (UNTESTED-UNTIL-ACCOUNT)")
            return None

    # ----- 1. POST /token (認証) -----

    def login(self) -> ApiResult:
        """POST /token で X-API-KEY を取得。

        body={"APIPassword": pw} → {"Token": "..."}。
        全例外を握って degraded=True で返す (落ちない)。
        """
        if not self._api_password:
            return ApiResult(False, error="KABU_API_PASSWORD not set", degraded=True)
        requests = self._requests()
        if requests is None:
            return ApiResult(False, error="requests unavailable", degraded=True)
        try:
            resp = requests.post(
                f"{self._base_url}/token",
                json={"APIPassword": self._api_password},
                headers={"Content-Type": "application/json"},
                timeout=self._timeout,
            )
            if resp.status_code != 200:
                return ApiResult(False, error=f"HTTP {resp.status_code}: {resp.text[:160]}",
                                 degraded=True, status_code=resp.status_code)
            token = resp.json().get("Token")
            if not token:
                return ApiResult(False, error="Token field missing", degraded=True,
                                 status_code=resp.status_code)
            self._token = token
            self._token_issued_at = datetime.now()
            logger.info("login OK env=%s token=...%s", self._env, token[-6:])
            return ApiResult(True, data={"token_tail": token[-6:]}, status_code=200)
        except Exception as e:  # noqa: BLE001 (graceful-degradation: 全例外を握る)
            logger.error("login exception: %s", e)
            return ApiResult(False, error=f"exception: {e}", degraded=True)

    def is_logged_in(self) -> bool:
        return self._token is not None

    def _auth_headers(self, with_content: bool = False) -> dict[str, str]:
        h = {"X-API-KEY": self._token or ""}
        if with_content:
            h["Content-Type"] = "application/json"
        return h

    # ----- 2. PUT /register (PUSH 配信銘柄登録, <=50) -----

    def register(self, symbols: list[str]) -> ApiResult:
        """PUT /register で PUSH 配信対象を登録 (<=50 銘柄)。

        body={"Symbols": [{"Symbol": "7203", "Exchange": 1}, ...]}。
        50 超は呼び出し側のバグなので degraded=False の明示エラーにする。
        """
        if not self.is_logged_in():
            return ApiResult(False, error="not logged in", degraded=True)
        if len(symbols) > REGISTER_MAX_SYMBOLS:
            return ApiResult(False,
                             error=f"too many symbols {len(symbols)} > {REGISTER_MAX_SYMBOLS}",
                             degraded=False)
        requests = self._requests()
        if requests is None:
            return ApiResult(False, error="requests unavailable", degraded=True)
        body = {"Symbols": [{"Symbol": s, "Exchange": self._exchange} for s in symbols]}
        try:
            resp = requests.put(
                f"{self._base_url}/register",
                json=body,
                headers=self._auth_headers(with_content=True),
                timeout=self._timeout,
            )
            if resp.status_code != 200:
                return ApiResult(False, error=f"HTTP {resp.status_code}: {resp.text[:160]}",
                                 degraded=True, status_code=resp.status_code)
            self._registered = list(symbols)
            data = resp.json() if resp.text else {}
            return ApiResult(True, data=data if isinstance(data, dict) else {"RegistList": data},
                             status_code=200)
        except Exception as e:  # noqa: BLE001
            logger.error("register exception: %s", e)
            return ApiResult(False, error=f"exception: {e}", degraded=True)

    # ----- 3. WebSocket PUSH consumer (CurrentPrice) -----

    def start_push(self, on_price: Callable[[dict[str, Any]], None] | None = None) -> ApiResult:
        """WebSocket /websocket に接続し PUSH (CurrentPrice 等) を受信し続ける。

        - websocket-client 未導入 / 接続失敗は degraded=True で返し、上位は
          GET /board の polling fallback に切替えるべき (graceful-degradation)。
        - 受信 msg は {"Symbol", "CurrentPrice", "CurrentPriceTime", ...} 想定 (要確認)。
        - on_price コールバックに dict をそのまま渡す。内部 _last_prices も更新。

        別スレッドで回す (near-close monitor をブロックしない)。
        """
        try:
            from websocket import WebSocketApp  # noqa: PLC0415
        except ImportError:
            logger.error("websocket-client not installed — PUSH unavailable, use /board fallback")
            return ApiResult(False, error="websocket-client unavailable", degraded=True)

        if self._ws_thread and self._ws_thread.is_alive():
            return ApiResult(True, data={"note": "already running"})

        def _on_message(_ws: Any, message: str) -> None:
            try:
                msg = json.loads(message)
                sym = str(msg.get("Symbol", ""))
                price = msg.get("CurrentPrice")
                if sym and price is not None:
                    self._last_prices[sym] = float(price)
                if on_price:
                    on_price(msg)
            except Exception as e:  # noqa: BLE001
                logger.warning("PUSH parse error: %s", e)

        def _on_error(_ws: Any, err: Any) -> None:
            logger.warning("PUSH error: %s", err)

        def _run() -> None:
            try:
                app = WebSocketApp(
                    self._ws_url,
                    on_message=_on_message,
                    on_error=_on_error,
                )
                # run_forever はブロッキング。stop イベントは close で割り込む想定 (Phase2精緻化)。
                while not self._ws_stop.is_set():
                    app.run_forever()
                    if self._ws_stop.is_set():
                        break
                    time.sleep(1.0)  # 切断 → 1s 後 再接続 (graceful)
            except Exception as e:  # noqa: BLE001
                logger.error("PUSH thread crashed: %s", e)

        self._ws_stop.clear()
        self._ws_thread = threading.Thread(target=_run, name="kabu-push", daemon=True)
        self._ws_thread.start()
        return ApiResult(True, data={"ws_url": self._ws_url})

    def stop_push(self) -> None:
        self._ws_stop.set()

    def last_push_price(self, symbol: str) -> float | None:
        return self._last_prices.get(str(symbol))

    # ----- 4. GET /board (PUSH fallback) -----

    def get_board(self, symbol: str) -> ApiResult:
        """GET /board/{symbol}@{exchange} で時価スナップショット。

        PUSH が切れている時の fallback。CurrentPrice 等を data に入れて返す。
        """
        if not self.is_logged_in():
            return ApiResult(False, error="not logged in", degraded=True)
        requests = self._requests()
        if requests is None:
            return ApiResult(False, error="requests unavailable", degraded=True)
        try:
            resp = requests.get(
                f"{self._base_url}/board/{symbol}@{self._exchange}",
                headers=self._auth_headers(),
                timeout=self._timeout,
            )
            if resp.status_code != 200:
                return ApiResult(False, error=f"HTTP {resp.status_code}: {resp.text[:160]}",
                                 degraded=True, status_code=resp.status_code)
            return ApiResult(True, data=resp.json(), status_code=200)
        except Exception as e:  # noqa: BLE001
            logger.error("get_board exception: %s", e)
            return ApiResult(False, error=f"exception: {e}", degraded=True)

    def current_price(self, symbol: str) -> float | None:
        """PUSH 優先、無ければ /board fallback で現値を返す (graceful chain)。"""
        p = self.last_push_price(symbol)
        if p is not None:
            return p
        r = self.get_board(symbol)
        if r.ok:
            cp = r.data.get("CurrentPrice")
            return float(cp) if cp is not None else None
        return None

    # ----- 5. POST /sendorder (引成後場 MOC) — ARM 無効で dry-run -----

    def build_moc_order(self, symbol: str, shares: int, side: str = SIDE_BUY) -> dict[str, Any]:
        """引成・後場 (FrontOrderType=16) の発注 body を組む (送信はしない)。

        この body 構築は副作用なしなので口座なしでも test できる。
        現物 (CashMargin=1)、後場引成、Price=0 (成行系)、当日有効。
        """
        fund_type = FUND_TYPE_SPOT_BUY if side == SIDE_BUY else FUND_TYPE_SPOT_SELL
        return {
            "Password": os.getenv("KABU_TRADE_PASSWORD", ""),  # 取引パスワード (要確認)
            "Symbol": symbol,
            "Exchange": self._exchange,            # TODO-VERIFY: 2026改定で SOR=9/27 か
            "SecurityType": SECURITY_TYPE_STOCK,
            "Side": side,
            "CashMargin": CASH_MARGIN_GENBUTSU,
            "DelivType": DELIV_TYPE_AUTO,
            "FundType": fund_type,
            "AccountType": self._account_type,
            "Qty": int(shares),
            "FrontOrderType": FRONT_CLOSE_PM,      # 16 = 引成(後場) = MOC ★
            "Price": 0,                            # 引成は 0
            "ExpireDay": 0,                        # 0 = 当日
        }

    def send_moc_order(self, symbol: str, shares: int, side: str = SIDE_BUY) -> ApiResult:
        """引成後場 (MOC) を発注。

        *** 安全ゲート ***
        ARM_REAL_ORDERS=False の間は **実送信しない**。組んだ body を data に入れ、
        ok=True, data["dry_run"]=True で返す (検証用)。
        ARM=True かつ login 済みのときだけ実 POST する (口座稼働 + Grove 承認後)。
        """
        if shares <= 0:
            return ApiResult(False, error="shares<=0", degraded=False)
        body = self.build_moc_order(symbol, shares, side)

        if not ARM_REAL_ORDERS:
            logger.info("DRY-RUN (ARM off): would POST /sendorder %s x%d side=%s FrontOrderType=16",
                        symbol, shares, side)
            return ApiResult(True, data={"dry_run": True, "order_body": body})

        # --- 以下は ARM=True かつ口座稼働後のみ到達 (今夜は未到達) ---
        if not self.is_logged_in():
            return ApiResult(False, error="not logged in", degraded=True)
        requests = self._requests()
        if requests is None:
            return ApiResult(False, error="requests unavailable", degraded=True)
        # rate limit (発注系 秒5件)
        with self._lock:
            elapsed = time.time() - self._last_order_time
            if elapsed < MIN_INTERVAL_ORDER:
                time.sleep(MIN_INTERVAL_ORDER - elapsed)
            self._last_order_time = time.time()
        try:
            resp = requests.post(
                f"{self._base_url}/sendorder",
                json=body,
                headers=self._auth_headers(with_content=True),
                timeout=self._timeout,
            )
            if resp.status_code != 200:
                return ApiResult(False, error=f"HTTP {resp.status_code}: {resp.text[:160]}",
                                 degraded=True, status_code=resp.status_code)
            data = resp.json()
            if not data.get("OrderId"):
                return ApiResult(False, error="OrderId missing", degraded=True, status_code=200)
            return ApiResult(True, data=data, status_code=200)
        except Exception as e:  # noqa: BLE001
            logger.error("send_moc_order exception: %s", e)
            return ApiResult(False, error=f"exception: {e}", degraded=True)


# ===========================================================================
# CLI (手動確認用 — 実発注しない)
# ===========================================================================

def _demo() -> None:
    print("kabu_adapter — UNTESTED-UNTIL-ACCOUNT scaffold (env=demo port 18081)")
    print("-" * 70)
    ad = KabuMocAdapter(env="demo")
    print(f"ARM_REAL_ORDERS = {ARM_REAL_ORDERS}  (must be False tonight)")
    print(f"base_url = {ad._base_url}")
    print(f"ws_url   = {ad._ws_url}")
    # body 構築は口座なしでも検証可能:
    body = ad.build_moc_order("7203", 300, SIDE_BUY)
    print(f"sample MOC order body: FrontOrderType={body['FrontOrderType']} "
          f"(16=引成後場), Side={body['Side']}, Qty={body['Qty']}, "
          f"CashMargin={body['CashMargin']}")
    # login は口座未稼働なので degraded で返るのが正常:
    r = ad.login()
    print(f"login() -> ok={r.ok} degraded={r.degraded} error={r.error!r}  "
          f"(degraded=True が正常: 口座/VM 未稼働)")
    # dry-run 発注:
    o = ad.send_moc_order("7203", 300, SIDE_BUY)
    print(f"send_moc_order() -> ok={o.ok} dry_run={o.data.get('dry_run')}  (実送信していない)")


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    _demo()
