"""Tests for src/broker/kabu_com.py — KabuComClient + MockKabuComClient.

Phase 1 skeleton tests:
- Interface 整合性 (BrokerBase 継承)
- Login flow (token取得 mock)
- 注文送信 (request body 検証)
- Mock の buy/sell/balance挙動
- 法人口座 (AccountType=12) 切替確認

Phase 2 (口座開設後) で追加予定:
- 実 API疎通テスト (demo環境)
- Order status poller
- /positions, /orders, /wallet 完全実装
- token refresh on 401
"""
from __future__ import annotations

from unittest.mock import patch, MagicMock

import pytest

from src.broker.tachibana import BrokerBase
from src.broker.kabu_com import (
    ACCOUNT_TYPE_HOJIN,
    ACCOUNT_TYPE_TOKUTEI,
    ENVS,
    KabuComClient,
    MockKabuComClient,
    SIDE_BUY,
    SIDE_SELL,
)


class TestInterfaceCompliance:
    """BrokerBase interface 互換性 (既存 main.py / monitor.py に挿せること)."""

    def test_kabu_inherits_broker_base(self):
        assert issubclass(KabuComClient, BrokerBase)

    def test_mock_inherits_broker_base(self):
        assert issubclass(MockKabuComClient, BrokerBase)

    def test_required_methods_exist(self):
        client = KabuComClient(api_password="test")
        for method in ("buy", "sell", "get_balance"):
            assert callable(getattr(client, method))


class TestEnvironment:
    def test_env_urls_localhost(self):
        """kabuステーション API は Windows VM 内 localhost で稼働."""
        assert ENVS["production"] == "http://localhost:18080/kabusapi"
        assert ENVS["demo"] == "http://localhost:18081/kabusapi"

    def test_default_env_is_demo(self):
        """安全のため未指定なら demo (検証口座)."""
        client = KabuComClient(api_password="x")
        assert client._env == "demo"

    def test_invalid_env_falls_back_to_demo(self):
        client = KabuComClient(api_password="x", env="invalid")
        assert client._env == "demo"


class TestLogin:
    @patch("src.broker.kabu_com.requests.post")
    def test_login_success_stores_token(self, mock_post):
        mock_post.return_value = MagicMock(status_code=200, json=lambda: {"Token": "abc123token"})
        client = KabuComClient(api_password="api_pass")
        ok = client.login()
        assert ok is True
        assert client.is_logged_in()
        assert client._token == "abc123token"
        # POST /token に APIPassword が送られたか
        call = mock_post.call_args
        assert "/token" in call.args[0]
        assert call.kwargs["json"] == {"APIPassword": "api_pass"}

    @patch("src.broker.kabu_com.requests.post")
    def test_login_failure_returns_false(self, mock_post):
        mock_post.return_value = MagicMock(status_code=401, text="Unauthorized")
        client = KabuComClient(api_password="bad")
        assert client.login() is False
        assert not client.is_logged_in()

    def test_login_without_password_fails(self):
        client = KabuComClient(api_password="")
        assert client.login() is False


class TestSendOrder:
    """POST /sendorder body の正確性 — Phase 1で重要 (実 API疎通前に request 形式を fix)."""

    @patch("src.broker.kabu_com.requests.post")
    def test_buy_sends_correct_body(self, mock_post):
        # /token と /sendorder の両方に reply
        mock_post.side_effect = [
            MagicMock(status_code=200, json=lambda: {"Token": "tok"}),
            MagicMock(status_code=200, json=lambda: {"OrderId": "ORD123"}),
        ]
        client = KabuComClient(api_password="p")
        client.login()
        result = client.buy("7203", 100)

        assert result["status"] == "ok"
        assert result["order_id"] == "ORD123"

        # /sendorder body 検証
        order_call = mock_post.call_args_list[1]
        body = order_call.kwargs["json"]
        assert body["Symbol"] == "7203"
        assert body["Side"] == SIDE_BUY
        assert body["Qty"] == 100
        assert body["FrontOrderType"] == 10  # 成行
        assert body["Price"] == 0  # 成行は 0
        # ヘッダで X-API-KEY が token
        assert order_call.kwargs["headers"]["X-API-KEY"] == "tok"

    @patch("src.broker.kabu_com.requests.post")
    def test_sell_uses_sell_side(self, mock_post):
        mock_post.side_effect = [
            MagicMock(status_code=200, json=lambda: {"Token": "tok"}),
            MagicMock(status_code=200, json=lambda: {"OrderId": "S456"}),
        ]
        client = KabuComClient(api_password="p")
        client.login()
        client.sell("4519", 200)

        body = mock_post.call_args_list[1].kwargs["json"]
        assert body["Side"] == SIDE_SELL
        assert body["Qty"] == 200

    def test_order_without_login_returns_error(self):
        client = KabuComClient(api_password="p")
        result = client.buy("7203", 100)
        assert result["status"] == "error"
        assert "Not logged in" in result["message"]


class TestCorporateAccount:
    """AGI法人化時のため AccountType=12 (法人) 切替確認 (Phase 2 で API側可否 要確認)."""

    @patch("src.broker.kabu_com.requests.post")
    def test_corporate_account_type_propagates_to_order(self, mock_post):
        mock_post.side_effect = [
            MagicMock(status_code=200, json=lambda: {"Token": "t"}),
            MagicMock(status_code=200, json=lambda: {"OrderId": "C1"}),
        ]
        client = KabuComClient(api_password="p", account_type=ACCOUNT_TYPE_HOJIN)
        client.login()
        client.buy("7203", 100)
        body = mock_post.call_args_list[1].kwargs["json"]
        assert body["AccountType"] == 12

    def test_default_account_type_is_tokutei(self):
        """個人特定口座がデフォルト (Phase 1)."""
        client = KabuComClient(api_password="p")
        assert client._account_type == ACCOUNT_TYPE_TOKUTEI == 4


class TestMockKabuComClient:
    """Mock の挙動: commission ¥0 を反映 (kabu SOR想定)."""

    def test_buy_succeeds(self):
        mock = MockKabuComClient(initial_balance=1_000_000.0)
        mock.set_price("7203", 2500.0)
        result = mock.buy("7203", 100)
        assert result["status"] == "ok"
        assert result["executed_price"] is not None
        assert 2490 < result["executed_price"] < 2510  # ±0.2% slippage range

    def test_buy_no_commission(self):
        """kabu Mock は commission ¥0 (立花は 22bps 控除あり)."""
        mock = MockKabuComClient(initial_balance=1_000_000.0)
        mock.set_price("7203", 2500.0)
        mock.buy("7203", 100)
        # 残高 = 1M - (2500 * 100) = 750,000、commission 控除なし
        # MockTachibana だと calc_commission 分減るが、Mock kabu は控除なし
        cost_at_min = 2500 * 0.998 * 100  # ~249,500
        cost_at_max = 2500 * 1.002 * 100  # ~250,500
        spent = 1_000_000.0 - mock.get_balance()
        assert cost_at_min <= spent <= cost_at_max  # commission 控除なし

    def test_sell_increases_balance(self):
        mock = MockKabuComClient(initial_balance=1_000_000.0)
        mock.set_price("7203", 2500.0)
        mock.buy("7203", 100)
        bal_after_buy = mock.get_balance()
        mock.sell("7203", 100)
        assert mock.get_balance() > bal_after_buy  # 売却で増える (commission ¥0で round trip ほぼ無損)

    def test_insufficient_balance(self):
        mock = MockKabuComClient(initial_balance=100_000.0)
        mock.set_price("7203", 2500.0)
        result = mock.buy("7203", 100)  # 250K必要、残100K
        assert result["status"] == "error"
        assert "Insufficient" in result["message"]
