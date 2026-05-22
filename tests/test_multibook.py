"""Phase 0: マルチブック・ペーパーエンジン + 適応サイジング検証。

なぜ: 真因 = kelly_size の100株丸めで ¥1000超銘柄が全シグナル0株 →
3年で実取引3件(全て約¥1000銘柄)。本テストは修正の核を固定する:
- flex=True で少額ブックが通常価格株を最低1単元買えること
- flex=False は従来挙動完全維持（既存テスト・dry-run・live不変）
- book_account が複利equity/拘束差引free_cashを正しく出すこと
- execution_node が book を記録すること
"""
from __future__ import annotations

from datetime import date

import pytest

from unittest.mock import patch

from src.kelly import kelly_size
from src.main import kelly_node, execution_node, run_paper_multibook, _cap_shares_by_cash
from src.broker.tachibana import MockTachibanaClient
from src.data.db import get_connection, book_account, calc_commission
from tests.test_scan_graph import _make_df


# === kelly_size: flex の効果（バグ修正の核） ===

class TestFlexSizing:
    def test_flex_buys_normal_priced_stock_when_strict_fails(self):
        """¥3000株 @ ¥1M c=3: 従来0株(バグ) → flexで最低1単元100株。"""
        assert kelly_size(3, 1_000_000, 3000.0) == 0          # 従来挙動(バグ)維持
        assert kelly_size(3, 1_000_000, 3000.0, flex=True) == 100  # 修正

    def test_flex_respects_cash_floor(self):
        """1単元すら買えない(cash < price*100)なら flex でも0。上限なしだが下限はcash。"""
        # balance ¥200k, ¥3000株 → 1単元=¥300k > cash → 0
        assert kelly_size(3, 200_000, 3000.0, flex=True) == 0

    def test_flex_false_is_unchanged(self):
        """flex=False は完全に従来式（大型ブック/dry-run/live/既存テスト不変）。"""
        for c in (3, 4, 5):
            assert kelly_size(c, 1_000_000, 1000.0) == kelly_size(c, 1_000_000, 1000.0, flex=False)
        assert kelly_size(3, 1_000_000, 50000.0, flex=False) == 0
        assert kelly_size(2, 1_000_000, 1000.0, flex=True) == 0  # consensus<3は flex でも0

    def test_large_book_strict_uses_fixed_pct(self):
        """大型ブック(¥30M)は固定10%規律: ¥3000株 → 30M*0.1/3000/100*100=1000株。"""
        assert kelly_size(3, 30_000_000, 3000.0, flex=False) == 1000


# === book_account: 複利equity と 拘束差引free_cash ===

class TestBookAccount:
    def test_empty_book(self, tmp_path):
        con = get_connection(str(tmp_path / "a.db"))
        equity, free, tk = book_account(con, "p1m", 1_000_000)
        con.close()
        assert equity == 1_000_000 and free == 1_000_000 and tk == set()

    def test_realized_compounds_and_open_is_committed(self, tmp_path):
        db = str(tmp_path / "b.db")
        con = get_connection(db)
        # closed: realized +5000 (pnlは手数料控除後の値をそのまま)
        con.execute(
            "INSERT INTO positions (ticker,entry_price,shares,entry_date,status,exit_price,"
            "exit_reason,pnl,commission,book) VALUES "
            "('1111',1000,100,?, 'closed',1060,'take_profit',5000,200,'p1m')",
            [date(2026, 5, 1)],
        )
        # open: committed = 2000*100 + 220 = 200220
        con.execute(
            "INSERT INTO positions (ticker,entry_price,shares,entry_date,status,commission,book) "
            "VALUES ('2222',2000,100,?, 'open',220,'p1m')",
            [date(2026, 5, 12)],
        )
        # 別ブックの行は混ざらない
        con.execute(
            "INSERT INTO positions (ticker,entry_price,shares,entry_date,status,commission,book) "
            "VALUES ('3333',5000,100,?, 'open',550,'p50m')",
            [date(2026, 5, 12)],
        )
        equity, free, tk = book_account(con, "p1m", 1_000_000)
        con.close()
        assert equity == 1_005_000          # 初期 + realized
        assert free == 1_005_000 - 200_220  # equity - committed(open)
        assert tk == {"2222"}


# === kelly_node マルチブック経路 ===

class TestKellyNodeMultibook:
    def _votes(self, *tickers):
        return {t: {"consensus": 3} for t in tickers}

    def test_small_flex_book_enters_normal_priced(self):
        """¥1Mブック(flex): ¥3000銘柄でも建つ。従来は position_size空。"""
        state = {
            "votes": self._votes("3000A"),
            "market_data": {"3000A": _make_df([3000.0] * 30)},
            "book": "p1m", "book_equity": 1_000_000.0,
            "book_free_cash": 1_000_000.0, "book_flex": True,
            "book_open_tickers": set(), "errors": [],
        }
        out = kelly_node(state)
        assert out["position_size"].get("3000A") == 100

    def test_large_strict_book_uses_pct(self):
        """Phase 0+ all-additive (2026-05-22): v2 router経由でEVS→cap_pct動的決定。
        加算合成化で bare consensus=3 の baseline cap が ~27% に上昇
        (F1=0.6 + F6 winrate prior 0.625 が加算で底上げ)。30M×0.27/3000≈2700株。
        旧乗算崩壊時代の 1200株から再度の意図的挙動変更。
        この baseline 高さは「weak setup の cap 上限」として 5/30 scipy で調整予定。
        """
        state = {
            "votes": self._votes("3000B"),
            "market_data": {"3000B": _make_df([3000.0] * 30)},
            "book": "p30m", "book_equity": 30_000_000.0,
            "book_free_cash": 30_000_000.0, "book_flex": False,
            "book_open_tickers": set(), "errors": [],
        }
        out = kelly_node(state)
        shares = out["position_size"].get("3000B", 0)
        # v2 additive: router-driven, expect 100-3500 range (consensus=3 baseline ~27%)
        assert 100 <= shares <= 3500, f"unexpected v2 size: {shares}"

    def test_cash_guard_caps_cumulative_deployment(self):
        """free_cash ¥350k・¥3000株2件: 1件目100株(¥300k)で現金尽き2件目は0。"""
        state = {
            "votes": {"AAA": {"consensus": 3}, "BBB": {"consensus": 3}},
            "market_data": {"AAA": _make_df([3000.0] * 30), "BBB": _make_df([3000.0] * 30)},
            "book": "p1m", "book_equity": 1_000_000.0,
            "book_free_cash": 350_000.0, "book_flex": True,
            "book_open_tickers": set(), "errors": [],
        }
        out = kelly_node(state)
        ps = out["position_size"]
        assert sum(s * 3000.0 for s in ps.values()) <= 350_000.0
        assert len(ps) == 1  # 1件しか入らない

    def test_skips_open_ticker_in_same_book(self):
        state = {
            "votes": self._votes("DUP"),
            "market_data": {"DUP": _make_df([3000.0] * 30)},
            "book": "p1m", "book_equity": 1_000_000.0,
            "book_free_cash": 1_000_000.0, "book_flex": True,
            "book_open_tickers": {"DUP"}, "errors": [],
        }
        out = kelly_node(state)
        assert "DUP" not in out["position_size"]

    def test_legacy_path_unchanged_without_book(self, tmp_path):
        """state に book 無し → 従来挙動（broker.get_balance, flex=False）。"""
        state = {
            "votes": self._votes("LEG"),
            "market_data": {"LEG": _make_df([3000.0] * 30)},
            "broker": MockTachibanaClient(initial_balance=1_000_000.0),
            "db_path": str(tmp_path / "leg.db"), "errors": [],
        }
        out = kelly_node(state)
        # ¥3000@¥1M strict → 0株 → 建たない（従来バグ挙動の保存＝後方互換の証明）
        assert out["position_size"] == {}


# === execution_node: book タグ記録 ===

def test_execution_tags_book(tmp_path):
    db = str(tmp_path / "exec.db")
    state = {
        "position_size": {"7777": 100},
        "broker": MockTachibanaClient(initial_balance=1_000_000.0),
        "market_data": {"7777": _make_df([2500.0] * 30)},
        "db_path": db, "book": "p5m", "errors": [],
    }
    execution_node(state)
    con = get_connection(db)
    row = con.execute(
        "SELECT book, ticker FROM positions WHERE status='open'"
    ).fetchone()
    con.close()
    assert row == ("p5m", "7777")


# === cash上限ガード: 手数料込み対称（M1） ===

class TestCashGuardCommission:
    def test_cap_excludes_when_commission_tips_over(self):
        """¥3000×100=¥30万。手数料330込みで cash¥300,000 では1単元入らない。"""
        comm = calc_commission(3000.0, 100)  # 3000*100*0.0011=330
        assert _cap_shares_by_cash(100, 3000.0, 300_000.0) == 0
        assert _cap_shares_by_cash(100, 3000.0, 300_000.0 + comm) == 100

    def test_kelly_node_deducts_commission_from_remaining(self):
        """2銘柄連続: 1件目で price*shares+手数料 を引き、2件目は残現金内に収まる。"""
        # free_cash ぴったり 1件分(¥30万+手数料)+α。2件目は入らないはず。
        comm = calc_commission(3000.0, 100)
        state = {
            "votes": {"AA": {"consensus": 3}, "BB": {"consensus": 3}},
            "market_data": {"AA": _make_df([3000.0] * 30), "BB": _make_df([3000.0] * 30)},
            "book": "p1m", "book_equity": 1_000_000.0,
            "book_free_cash": 300_000.0 + comm + 50_000.0,  # 1件+残¥5万(1単元不可)
            "book_flex": True, "book_open_tickers": set(), "errors": [],
        }
        out = kelly_node(state)
        ps = out["position_size"]
        assert len(ps) == 1
        # 約定総コスト（手数料込み）が free_cash 以内
        total = sum(s * 3000.0 + calc_commission(3000.0, s) for s in ps.values())
        assert total <= 300_000.0 + comm + 50_000.0


# === open保有状態のブック会計連携（C1ギャップ補填） ===

def test_book_account_then_kelly_node_with_open(tmp_path):
    """open建玉が free_cash を拘束 → 次サイクルの kelly_node がその残内で建てる。"""
    db = str(tmp_path / "open.db")
    con = get_connection(db)
    # p1m に open 1件: committed = 2000*100 + 220 = 200,220
    con.execute(
        "INSERT INTO positions (ticker,entry_price,shares,entry_date,status,commission,book) "
        "VALUES ('OPEN1',2000,100,?, 'open',220,'p1m')",
        [date(2026, 5, 12)],
    )
    equity, free_cash, open_tk = book_account(con, "p1m", 1_000_000)
    con.close()
    assert open_tk == {"OPEN1"}
    assert free_cash == 1_000_000 - 200_220

    state = {
        "votes": {"NEW1": {"consensus": 3}, "OPEN1": {"consensus": 3}},
        "market_data": {"NEW1": _make_df([3000.0] * 30), "OPEN1": _make_df([2000.0] * 30)},
        "book": "p1m", "book_equity": equity, "book_free_cash": free_cash,
        "book_flex": True, "book_open_tickers": open_tk, "errors": [],
    }
    out = kelly_node(state)
    ps = out["position_size"]
    assert "OPEN1" not in ps  # 既存建玉は重複建てしない
    # 新規は free_cash(≈¥80万) の範囲内
    deployed = sum(s * 3000.0 + calc_commission(3000.0, s) for s in ps.values())
    assert deployed <= free_cash


# === free_cash<0 のサイレントclamp禁止（M2） ===

def test_negative_free_cash_book_is_skipped(tmp_path):
    """実現損が初期資金超過 → そのブックは停止しエラー記録、他ブックは正常稼働。"""
    db = str(tmp_path / "neg.db")
    con = get_connection(db)
    # p1m に巨額実現損: realized = -1,500,000 → equity=-500,000, free_cash<0
    con.execute(
        "INSERT INTO positions (ticker,entry_price,shares,entry_date,status,exit_price,"
        "exit_reason,pnl,commission,book) VALUES "
        "('LOSS',5000,100,?, 'closed',2000,'stop_loss',-1500000,300,'p1m')",
        [date(2026, 5, 1)],
    )
    con.close()

    fake_md = {"7203": _make_df([800.0] * 30)}  # ¥800株（少額ブックでも買える）
    # p4=True: treatment(regime_filter)ブックでも建つ＝負free_cashロジックを
    # A/B overlay と独立に検証（本テストの意図は free_cash<0 のskip）
    fake_votes = {"7203": {"consensus": 3, "p4": True}}

    with patch("src.main.ingestion_node", return_value={
                "market_data": fake_md, "nikkei_change": 0.0,
                "nikkei_ma25_dev": None, "errors": []}), \
         patch("src.main.voting_node", return_value={"votes": fake_votes, "errors": []}), \
         patch("src.monitor.run_monitor_cycle", return_value={"exit_decisions": [], "errors": []}):
        r = run_paper_multibook(db_path=db)

    assert any("negative_free_cash" in e and "p1m" in e for e in r["errors"])
    con = get_connection(db)
    p1m_open = con.execute(
        "SELECT count(*) FROM positions WHERE book='p1m' AND status='open'"
    ).fetchone()[0]
    p5m_open = con.execute(
        "SELECT count(*) FROM positions WHERE book='p5m' AND status='open'"
    ).fetchone()[0]
    con.close()
    assert p1m_open == 0          # 停止したブックは建玉ゼロ
    assert p5m_open >= 1          # 他ブックは正常稼働（¥800株を建てた）


# === A/B regime_filter（treatmentブックは弱気p4時のみ建玉）===

class TestRegimeFilterAB:
    def _state(self, regime_filter):
        return {
            "votes": {
                "BULL": {"consensus": 4, "p4": False},  # 強気: p4=False
                "BEAR": {"consensus": 4, "p4": True},    # 弱気: p4=True
            },
            "market_data": {"BULL": _make_df([3000.0] * 30),
                            "BEAR": _make_df([3000.0] * 30)},
            "book": "pX", "book_equity": 30_000_000.0,
            "book_free_cash": 30_000_000.0, "book_flex": False,
            "book_regime_filter": regime_filter,
            "book_open_tickers": set(), "errors": [],
        }

    def test_treatment_enters_only_bearish(self):
        out = kelly_node(self._state(regime_filter=True))
        ps = out["position_size"]
        assert "BEAR" in ps and "BULL" not in ps  # p4必須

    def test_control_enters_both(self):
        out = kelly_node(self._state(regime_filter=False))
        ps = out["position_size"]
        assert "BEAR" in ps and "BULL" in ps  # 従来通り

    def test_books_ab_split(self):
        from config.books import BOOKS
        tr = {b.book_id for b in BOOKS if b.regime_filter}
        ct = {b.book_id for b in BOOKS if not b.regime_filter}
        assert tr == {"p5m", "p30m"}
        assert ct == {"p1m", "p10m", "p50m"}


def test_execution_defaults_legacy_book(tmp_path):
    """book 未指定（従来 run_scan_cycle 経路）は 'legacy' タグ。"""
    db = str(tmp_path / "exec2.db")
    state = {
        "position_size": {"8888": 100},
        "broker": MockTachibanaClient(initial_balance=1_000_000.0),
        "market_data": {"8888": _make_df([2500.0] * 30)},
        "db_path": db, "errors": [],
    }
    execution_node(state)
    con = get_connection(db)
    row = con.execute("SELECT book FROM positions WHERE status='open'").fetchone()
    con.close()
    assert row == ("legacy",)
