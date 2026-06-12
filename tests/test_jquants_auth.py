"""J-Quants mail/pass認証のテスト。"""
from __future__ import annotations


def test_get_jquants_client_with_mail_pass(monkeypatch):
    """JQUANTS_MAILADDRESS + JQUANTS_PASSWORD でclientが返る。"""
    monkeypatch.setenv("JQUANTS_MAILADDRESS", "test@example.com")
    monkeypatch.setenv("JQUANTS_PASSWORD", "dummy")
    monkeypatch.delenv("JQUANTS_API_KEY", raising=False)

    from src.data import jquants
    client = jquants._get_jquants_client()
    assert client is not None, "mail/pass should produce a client"


def test_get_jquants_client_with_api_key(monkeypatch):
    """JQUANTS_API_KEY 経由でも引き続きclientが返る（後方互換）。"""
    monkeypatch.setenv("JQUANTS_API_KEY", "dummy_key")
    monkeypatch.delenv("JQUANTS_MAILADDRESS", raising=False)
    monkeypatch.delenv("JQUANTS_PASSWORD", raising=False)

    from src.data import jquants
    client = jquants._get_jquants_client()
    assert client is not None


def test_get_jquants_client_missing_creds(monkeypatch):
    """全認証情報なしならNone。"""
    monkeypatch.delenv("JQUANTS_API_KEY", raising=False)
    monkeypatch.delenv("JQUANTS_REFRESH_TOKEN", raising=False)
    monkeypatch.delenv("JQUANTS_MAILADDRESS", raising=False)
    monkeypatch.delenv("JQUANTS_PASSWORD", raising=False)

    from src.data import jquants
    client = jquants._get_jquants_client()
    assert client is None
