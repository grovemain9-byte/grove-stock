"""Tests for src/sizing/router.py — PLT lookup + EVS fallback + ε-greedy."""
from __future__ import annotations

import random
from datetime import date, datetime

import duckdb
import pytest

from src.sizing.evs import EVSComponents
from src.sizing.plt import (
    CellStats,
    ensure_plt_table,
    upsert_cell,
)
from src.sizing.router import (
    EXPLORE_CAP_MAX,
    EXPLORE_CAP_MIN,
    SINGLE_TICKER_MAX,
    SizingDecision,
    compute_portfolio_aware_shares,
    decide_cap,
    shares_from_decision,
)


def _evs_components(cap_pct: float = 0.30, f2: float = 0.6, f4: float = 0.4) -> EVSComponents:
    """Helper: minimal EVSComponents."""
    return EVSComponents(
        f1_signal_strength=0.6, f2_deviation_depth=f2, f3_rsi_oversold=0.5,
        f4_bb_penetration=f4, f5_volume_decline=0.5,
        f6_bayes_winrate=0.625, f7_market_regime=1.0, f8_realized_vol_filter=0.6,
        f9_concentration_penalty=0.0, f10_liquidity_filter=0.9,
        edge_score=0.5, prior_adj=0.34,
        evs_total=0.25, cap_pct=cap_pct,
    )


def _hot_cell(cell_id: str, cap: float = 0.45) -> CellStats:
    return CellStats(
        cell_id=cell_id, n_samples=25, n_wins=18,
        avg_pnl_pct=0.012, std_pnl_pct=0.02,
        avg_win_pct=0.025, avg_loss_pct=-0.040,
        shrunk_win_rate=0.72, kelly_fraction=0.50,
        recommended_cap_pct=cap,
        last_updated=datetime(2026, 5, 21), confidence="hot",
    )


def _cold_cell(cell_id: str) -> CellStats:
    return CellStats(
        cell_id=cell_id, n_samples=2, n_wins=1,
        avg_pnl_pct=0.005, std_pnl_pct=0.01,
        avg_win_pct=0.02, avg_loss_pct=-0.01,
        shrunk_win_rate=0.60, kelly_fraction=0.10,
        recommended_cap_pct=0.10,
        last_updated=datetime(2026, 5, 21), confidence="cold",
    )


@pytest.fixture
def con():
    c = duckdb.connect(":memory:")
    ensure_plt_table(c)
    yield c
    c.close()


class TestDecideCap:
    def test_missing_cell_falls_to_evs(self, con):
        """No PLT row → use EVS fallback cap."""
        comp = _evs_components(cap_pct=0.30)
        decision = decide_cap(
            con=con, components=comp,
            consensus=4, rsi=20.0, nikkei_ma25_dev=-0.02,
            ticker="6758", decided_date=date(2026, 5, 22),
        )
        assert decision.source == "evs_fallback"
        assert decision.cap_pct == 0.30
        assert decision.cell_confidence == "cold"
        assert decision.fallback_used is True
        assert decision.exploration_flag is False

    def test_hot_cell_uses_plt_cap_exploit(self, con):
        """Hot cell + non-explore draw → use PLT cap."""
        cell_id = "c4_d1_r1_b1_bear_tech"
        upsert_cell(con, _hot_cell(cell_id, cap=0.45))
        # Force exploitation: rng draws > eps
        rng = random.Random()
        rng.random = lambda: 0.99  # type: ignore[method-assign]
        comp = _evs_components(cap_pct=0.30, f2=0.6, f4=0.4)
        decision = decide_cap(
            con=con, components=comp,
            consensus=4, rsi=20.0, nikkei_ma25_dev=-0.02,
            ticker="6758", decided_date=date(2026, 5, 22),
            rng=rng,
        )
        assert decision.source == "plt_hot"
        assert decision.cap_pct == 0.45
        assert decision.exploration_flag is False
        assert decision.fallback_used is False

    def test_hot_cell_explore_uses_evs(self, con):
        """Hot cell + explore draw (<eps) → use EVS cap as exploration."""
        cell_id = "c4_d1_r1_b1_bear_tech"
        upsert_cell(con, _hot_cell(cell_id, cap=0.45))
        rng = random.Random()
        rng.random = lambda: 0.01  # type: ignore[method-assign]
        comp = _evs_components(cap_pct=0.30, f2=0.6, f4=0.4)
        decision = decide_cap(
            con=con, components=comp,
            consensus=4, rsi=20.0, nikkei_ma25_dev=-0.02,
            ticker="6758", decided_date=date(2026, 5, 22),
            rng=rng,
        )
        assert decision.source == "exploration"
        assert decision.cap_pct == 0.30
        assert decision.exploration_flag is True
        assert decision.fallback_used is False

    def test_cold_cell_no_explore_uses_evs(self, con):
        """Cold cell + non-explore → use EVS fallback."""
        cell_id = "c3_d0_r2_b0_bull_other"
        upsert_cell(con, _cold_cell(cell_id))
        rng = random.Random()
        rng.random = lambda: 0.99  # type: ignore[method-assign]
        comp = _evs_components(cap_pct=0.20, f2=0.2, f4=0.1)
        decision = decide_cap(
            con=con, components=comp,
            consensus=3, rsi=30.0, nikkei_ma25_dev=0.01,
            ticker="9999", decided_date=date(2026, 5, 22),
            rng=rng,
        )
        assert decision.source == "plt_cold"
        assert decision.cap_pct == 0.20  # EVS used
        assert decision.cell_confidence == "cold"
        assert decision.fallback_used is True

    def test_cold_cell_explore_random_uniform(self, con):
        """Cold cell + explore → random uniform [EXPLORE_CAP_MIN, EXPLORE_CAP_MAX]."""
        cell_id = "c3_d0_r2_b0_bull_other"
        upsert_cell(con, _cold_cell(cell_id))

        class FakeRNG:
            def __init__(self):
                self.calls = 0
            def random(self):
                return 0.01  # forces explore
            def uniform(self, a, b):
                return 0.35  # mid-range result

        comp = _evs_components(cap_pct=0.20, f2=0.2, f4=0.1)
        decision = decide_cap(
            con=con, components=comp,
            consensus=3, rsi=30.0, nikkei_ma25_dev=0.01,
            ticker="9999", decided_date=date(2026, 5, 22),
            rng=FakeRNG(),  # type: ignore[arg-type]
        )
        assert decision.source == "exploration"
        assert decision.cap_pct == 0.35
        assert decision.exploration_flag is True
        assert EXPLORE_CAP_MIN <= decision.cap_pct <= EXPLORE_CAP_MAX

    def test_deterministic_rng_reproducible(self, con):
        """Same (ticker, date) → same exploration decision across calls."""
        cell_id = "c4_d1_r1_b1_bear_tech"
        upsert_cell(con, _hot_cell(cell_id, cap=0.45))
        comp = _evs_components(cap_pct=0.30, f2=0.6, f4=0.4)
        kwargs = dict(
            con=con, components=comp,
            consensus=4, rsi=20.0, nikkei_ma25_dev=-0.02,
            ticker="6758", decided_date=date(2026, 5, 22),
        )
        d1 = decide_cap(**kwargs)
        d2 = decide_cap(**kwargs)
        # Both should yield same source (either both explore or both exploit)
        assert d1.source == d2.source
        assert d1.cap_pct == d2.cap_pct


class TestSharesFromDecision:
    def test_basic_router_cap(self):
        d = SizingDecision(
            cap_pct=0.30, source="plt_hot", cell_id="x", cell_n_samples=10,
            cell_confidence="warm", exploration_flag=False, fallback_used=False,
        )
        shares, reason = shares_from_decision(decision=d, capital=1_000_000, price=3000, flex=False)
        # 1M * 0.3 = 300K / 3000 = 100 shares
        assert shares == 100
        assert reason == "router_cap"

    def test_flex_min_unit_when_capped_zero(self):
        d = SizingDecision(
            cap_pct=0.10, source="evs_fallback", cell_id="x", cell_n_samples=0,
            cell_confidence="cold", exploration_flag=False, fallback_used=True,
        )
        # cap_value = 1M * 0.10 = 100K, price 4000 → 0 shares from cap, flex saves
        shares, reason = shares_from_decision(decision=d, capital=1_000_000, price=4000, flex=True)
        assert shares == 100
        assert reason == "flex_min_unit"

    def test_zero_cap_blocks_flex(self):
        """cap_pct=0 → even flex cannot fire (no edge signal)."""
        d = SizingDecision(
            cap_pct=0.0, source="evs_fallback", cell_id="x", cell_n_samples=0,
            cell_confidence="cold", exploration_flag=False, fallback_used=True,
        )
        shares, reason = shares_from_decision(decision=d, capital=1_000_000, price=4000, flex=True)
        assert shares == 0

    def test_insufficient_capital_for_unit(self):
        d = SizingDecision(
            cap_pct=0.20, source="evs_fallback", cell_id="x", cell_n_samples=0,
            cell_confidence="cold", exploration_flag=False, fallback_used=True,
        )
        # 100K * 0.2 = 20K, price 4000 → 0 shares, even flex can't (need 400K for 1 unit)
        shares, reason = shares_from_decision(decision=d, capital=100_000, price=4000, flex=True)
        assert shares == 0
        assert reason == "shares_zero"


# === Portfolio-aware 3-layer constraints (2026-05-25) ===

class TestPortfolioAwareShares:
    """compute_portfolio_aware_shares: Layer 1 slot / Layer 2 cash / Layer 3 ticker."""

    def _decision(self, cap_pct: float = 0.30) -> SizingDecision:
        return SizingDecision(
            cap_pct=cap_pct, source="plt_hot", cell_id="x", cell_n_samples=20,
            cell_confidence="hot", exploration_flag=False, fallback_used=False,
        )

    def test_layer1_slot_limits_when_per_slot_smaller_than_cap(self):
        """残slots=10, free_cash=¥10M → per_slot ¥1M < cap_value ¥15M → slot_limited."""
        d = self._decision(cap_pct=0.30)  # 50M*0.3=15M原案
        shares, reason, eff = compute_portfolio_aware_shares(
            decision=d, capital=50_000_000, free_cash=10_000_000,
            price=1000, flex=False, slots_left=10,
        )
        # per_slot = 10M/10 = 1M, ticker_ceiling = 50M*0.15 = 7.5M
        # → slot wins (smallest), 1M / 1000 = 1000 shares
        assert reason == "slot_limited"
        assert shares == 1000
        assert abs(eff - 0.02) < 0.001  # 1M/50M = 2%

    def test_layer3_single_ticker_ceiling_caps_at_15pct(self):
        """capital=50M, free_cash=40M, slots=2 (大kuhuni) → ticker_ceiling 7.5M が効く."""
        d = self._decision(cap_pct=0.50)  # 50M*0.5=25M原案
        shares, reason, eff = compute_portfolio_aware_shares(
            decision=d, capital=50_000_000, free_cash=40_000_000,
            price=1000, flex=False, slots_left=2,
        )
        # per_slot = 40M/2 = 20M, ticker_ceiling = 7.5M → ticker wins
        assert reason == "ticker_ceiling"
        assert shares == 7500  # 7.5M / 1000
        assert abs(eff - SINGLE_TICKER_MAX) < 0.001

    def test_layer2_cash_limited_when_free_cash_small(self):
        """free_cash=¥500K, slots=1 → cap_value=15M, per_slot=500K, ticker_ceiling=7.5M
        → 500K (cash) wins."""
        d = self._decision(cap_pct=0.30)
        shares, reason, eff = compute_portfolio_aware_shares(
            decision=d, capital=50_000_000, free_cash=500_000,
            price=1000, flex=False, slots_left=1,
        )
        # per_slot = 500K/1 = 500K (= free_cash). cash_limited only when slot ≠ cash.
        # Here per_slot == free_cash, so reason stays "slot_limited" (first match)
        assert reason in ("slot_limited", "cash_limited")
        assert shares == 500  # 500K / 1000

    def test_p50m_real_case_4th_trade_should_not_zero(self):
        """forward paper の p50m 病理再現: 3件入った後の4件目が shares_zero になっていた問題.

        現実値: equity=50M, 3件で ¥45M 使い、free_cash=¥5M、slots_left=17 (=20-3)
        従来: cap_value = 50M * 0.30 = 15M → 5M(free) 超えて shares_zero
        修正後: per_slot = 5M/17 = ¥294K → 294K/1000 = 200株 (建つ)
        """
        d = self._decision(cap_pct=0.30)
        shares, reason, eff = compute_portfolio_aware_shares(
            decision=d, capital=50_000_000, free_cash=5_000_000,
            price=1000, flex=False, slots_left=17,
        )
        assert shares > 0  # 旧コードでは 0 だった
        assert reason == "slot_limited"

    def test_zero_cap_returns_zero_regardless(self):
        d = self._decision(cap_pct=0.0)
        shares, reason, eff = compute_portfolio_aware_shares(
            decision=d, capital=50_000_000, free_cash=50_000_000,
            price=1000, flex=True, slots_left=20,
        )
        assert shares == 0
        assert reason == "shares_zero"

    def test_flex_min_unit_when_cap_layers_zero_it(self):
        """flex=True で 3層cap が 0株まで圧縮しても free_cash で1単元買えれば建つ."""
        d = self._decision(cap_pct=0.50)
        # capital=1M, free_cash=400K, slots=20 → per_slot=20K → cap_layers cuts to 0株
        # But free_cash 400K >= price*100=400K → flex_min_unit
        shares, reason, eff = compute_portfolio_aware_shares(
            decision=d, capital=1_000_000, free_cash=400_000,
            price=4000, flex=True, slots_left=20,
        )
        assert shares == 100
        assert reason == "flex_min_unit"

    def test_no_free_cash_returns_zero(self):
        d = self._decision(cap_pct=0.30)
        shares, reason, eff = compute_portfolio_aware_shares(
            decision=d, capital=50_000_000, free_cash=0,
            price=1000, flex=True, slots_left=20,
        )
        assert shares == 0
        assert reason == "shares_zero"
