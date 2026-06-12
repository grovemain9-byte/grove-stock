"""Issue #2: TachibanaClient + MockTachibanaClient テスト。

受け入れ基準:
- MockClientでbuy→sellの1サイクルが正常完走すること
- 残高が正しく更新されること
"""
import pytest
from src.broker.tachibana import MockTachibanaClient, TachibanaClient, BrokerBase


# --- インターフェース ---

class TestInterface:
    def test_mock_is_broker_base(self):
        client = MockTachibanaClient()
        assert isinstance(client, BrokerBase)

    def test_real_is_broker_base(self):
        client = TachibanaClient(user_id="test", password="test")
        assert isinstance(client, BrokerBase)


# --- MockTachibanaClient ---

class TestMockBuy:
    def test_buy_returns_ok(self):
        client = MockTachibanaClient(initial_balance=1_000_000.0)
        client.set_price("4519", 8000.0)
        result = client.buy("4519", 100)
        assert result["status"] == "ok"
        assert "executed_price" in result
        assert "executed_at" in result

    def test_buy_price_within_range(self):
        client = MockTachibanaClient()
        client.set_price("4519", 10000.0)
        for _ in range(50):
            result = client.buy("4519", 1)
            assert 9950.0 <= result["executed_price"] <= 10050.0

    def test_buy_deducts_balance(self):
        client = MockTachibanaClient(initial_balance=1_000_000.0)
        client.set_price("4519", 8000.0)
        initial = client.get_balance()
        result = client.buy("4519", 100)
        cost = result["executed_price"] * 100
        assert client.get_balance() == pytest.approx(initial - cost)

    def test_buy_insufficient_balance(self):
        client = MockTachibanaClient(initial_balance=10_000.0)
        client.set_price("4519", 8000.0)
        result = client.buy("4519", 100)  # 800,000円必要
        assert result["status"] == "error"


class TestMockSell:
    def test_sell_returns_ok(self):
        client = MockTachibanaClient()
        client.set_price("4519", 8000.0)
        client.buy("4519", 100)
        result = client.sell("4519", 100)
        assert result["status"] == "ok"

    def test_sell_adds_balance(self):
        client = MockTachibanaClient(initial_balance=1_000_000.0)
        client.set_price("4519", 8000.0)
        client.buy("4519", 100)
        balance_after_buy = client.get_balance()
        result = client.sell("4519", 100)
        proceeds = result["executed_price"] * 100
        assert client.get_balance() == pytest.approx(balance_after_buy + proceeds)


class TestMockCycle:
    def test_buy_sell_full_cycle(self):
        """buy→sell 1サイクルの正常完走。"""
        client = MockTachibanaClient(initial_balance=1_000_000.0)
        client.set_price("4519", 8000.0)

        # Buy
        buy_result = client.buy("4519", 100)
        assert buy_result["status"] == "ok"
        balance_after_buy = client.get_balance()
        assert balance_after_buy < 1_000_000.0

        # Sell
        client.set_price("4519", 8500.0)  # 値上がり
        sell_result = client.sell("4519", 100)
        assert sell_result["status"] == "ok"
        balance_after_sell = client.get_balance()
        assert balance_after_sell > balance_after_buy

    def test_multiple_tickers(self):
        """複数銘柄の同時保有。"""
        client = MockTachibanaClient(initial_balance=5_000_000.0)
        client.set_price("4519", 8000.0)
        client.set_price("6758", 3000.0)

        r1 = client.buy("4519", 100)
        r2 = client.buy("6758", 200)
        assert r1["status"] == "ok"
        assert r2["status"] == "ok"

        r3 = client.sell("4519", 100)
        r4 = client.sell("6758", 200)
        assert r3["status"] == "ok"
        assert r4["status"] == "ok"

    def test_partial_sell(self):
        """100株買って50株だけ売る。"""
        client = MockTachibanaClient(initial_balance=1_000_000.0)
        client.set_price("4519", 5000.0)
        client.buy("4519", 100)
        result = client.sell("4519", 50)
        assert result["status"] == "ok"


class TestMockBalance:
    def test_initial_balance(self):
        client = MockTachibanaClient(initial_balance=500_000.0)
        assert client.get_balance() == 500_000.0

    def test_balance_consistency(self):
        """buy→sell後の残高が合理的な範囲。"""
        client = MockTachibanaClient(initial_balance=1_000_000.0)
        client.set_price("4502", 5000.0)
        client.buy("4502", 100)
        client.set_price("4502", 5000.0)
        client.sell("4502", 100)
        # ±0.5%のスリッページで、残高はほぼ元通り（±1%以内）
        assert 990_000 <= client.get_balance() <= 1_010_000


# --- TachibanaClient 設定テスト ---

class TestTachibanaConfig:
    def test_demo_env_default(self):
        client = TachibanaClient(user_id="test", password="test")
        assert client._env == "demo"
        assert "demo-kabuka" in client._base_url

    def test_production_requires_second_password(self, monkeypatch):
        monkeypatch.delenv("TACHIBANA_SECOND_PASSWORD", raising=False)
        with pytest.raises(ValueError, match="TACHIBANA_SECOND_PASSWORD"):
            TachibanaClient(user_id="test", password="test", env="production")

    def test_production_with_second_password(self):
        client = TachibanaClient(
            user_id="test", password="test",
            second_password="pass2", env="production"
        )
        assert client._env == "production"
        assert "demo" not in client._base_url

    def test_invalid_env_falls_back_to_demo(self):
        client = TachibanaClient(user_id="test", password="test", env="invalid")
        assert client._env == "demo"
