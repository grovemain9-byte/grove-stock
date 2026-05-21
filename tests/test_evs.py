"""Tests for src/sizing/evs.py — Expected Value Score multi-factor sizing.

Phase 0 design (Grove 2026-05-21):
- Weights: F1=0.4, F2=0.3, F3=0.2, F4=0.1
- Cap: linear cap_pct = 0.10 + 0.60 × EVS
- Bayes prior: Beta(5, 3) → 0.625 prior mean
"""
from __future__ import annotations

import pytest

from src.sizing.evs import (
    DEFAULT_WEIGHTS,
    EVSComponents,
    bayes_winrate,
    bb_penetration,
    cap_function,
    compute_evs,
    concentration_penalty,
    deviation_depth,
    evs_size,
    liquidity_filter,
    market_regime,
    realized_vol_filter,
    rsi_oversold,
    signal_strength,
    volume_decline_intensity,
)
from src.sizing import evs as evs_module


# ============================================================================
# Factor unit tests
# ============================================================================

class TestSignalStrength:
    def test_consensus_3_baseline(self):
        assert signal_strength(3) == pytest.approx(0.60)

    def test_consensus_4(self):
        assert signal_strength(4) == pytest.approx(0.80)

    def test_consensus_5_full_with_bonus(self):
        """5/5: base 1.0 + bonus 0.20 → capped at 1.0."""
        assert signal_strength(5) == pytest.approx(1.0)

    def test_consensus_zero(self):
        assert signal_strength(0) == 0.0

    def test_consensus_negative_safety(self):
        assert signal_strength(-1) == 0.0


class TestDeviationDepth:
    def test_exact_threshold(self):
        """-7% actual at -7% threshold → ratio 1.0 → 0.5."""
        assert deviation_depth(-0.07, -0.07) == pytest.approx(0.50)

    def test_strong_breach(self):
        """-14% at -7% → ratio 2.0 → 1.0 (capped)."""
        assert deviation_depth(-0.14, -0.07) == pytest.approx(1.0)

    def test_extreme_breach_capped(self):
        """-21% at -7% → ratio 3.0 → still capped at 1.0."""
        assert deviation_depth(-0.21, -0.07) == pytest.approx(1.0)

    def test_no_breach(self):
        """-5% actual at -7% threshold → ratio 0.714 → /2 → 0.357."""
        assert deviation_depth(-0.05, -0.07) == pytest.approx(0.357, abs=0.01)

    def test_positive_deviation_zero(self):
        """Above MA25 → not a逆張り setup → 0."""
        assert deviation_depth(0.05, -0.07) == 0.0


class TestRsiOversold:
    def test_rsi_at_threshold(self):
        assert rsi_oversold(35.0) == 0.0

    def test_rsi_deep_oversold(self):
        """RSI=20 → (35-20)/35 ≈ 0.43."""
        assert rsi_oversold(20.0) == pytest.approx(15 / 35)

    def test_rsi_extreme_zero(self):
        assert rsi_oversold(0.0) == pytest.approx(1.0)

    def test_rsi_overbought_zero(self):
        assert rsi_oversold(70.0) == 0.0


class TestBBPenetration:
    def test_at_lower_band(self):
        assert bb_penetration(close=1000.0, bb_lower=1000.0) == 0.0

    def test_1pct_below(self):
        """1% below → 1% × 50 = 0.5."""
        assert bb_penetration(close=990.0, bb_lower=1000.0) == pytest.approx(0.50)

    def test_2pct_below_full_credit(self):
        assert bb_penetration(close=980.0, bb_lower=1000.0) == pytest.approx(1.0)

    def test_above_lower_band_zero(self):
        assert bb_penetration(close=1010.0, bb_lower=1000.0) == 0.0


class TestVolumeDecline:
    def test_strong_decline_monotonic(self):
        """50% decline, v[-3]>v[-2]>v[-1] → 1.0."""
        assert volume_decline_intensity(2000, 1500, 1000) == pytest.approx(1.0)

    def test_mild_decline_monotonic(self):
        """25% decline, monotonic → 0.5."""
        assert volume_decline_intensity(1000, 850, 750) == pytest.approx(0.5)

    def test_decline_but_not_monotonic_halved(self):
        """Net decline but v[-2] > v[-3]: shape mismatch, score halved."""
        # v[-3]=1000, v[-2]=1500, v[-1]=900 → decline 10%, non-monotonic → 0.10
        result = volume_decline_intensity(1000, 1500, 900)
        assert result == pytest.approx(0.10, abs=0.01)

    def test_increasing_volume_zero(self):
        """v[-3]<v[-1] → 0."""
        assert volume_decline_intensity(800, 900, 1000) == 0.0

    def test_zero_safety(self):
        assert volume_decline_intensity(0, 0, 0) == 0.0


class TestRealizedVolFilter:
    def test_zero_vol_full_credit(self):
        assert realized_vol_filter(0.0) == 1.0

    def test_normal_vol_partial_credit(self):
        """20% vol → 1 - 0.2/0.5 = 0.6."""
        assert realized_vol_filter(0.20) == pytest.approx(0.60)

    def test_extreme_vol_floor_zero(self):
        assert realized_vol_filter(0.80) == 0.0

    def test_none_uncertainty(self):
        assert realized_vol_filter(None) == 0.85


class TestLiquidityFilter:
    def test_high_volume_full_credit(self):
        """log(1.2M) ≈ 14 → 1.0."""
        import math
        assert liquidity_filter(math.exp(14.0)) == pytest.approx(1.0)

    def test_mid_volume(self):
        """log(1000) ≈ 6.9 → 0.49."""
        import math
        assert liquidity_filter(math.exp(7.0)) == pytest.approx(0.5)

    def test_thin_volume_low(self):
        """avg_volume=100株/日 → log(100)/14 ≈ 0.33."""
        import math
        assert liquidity_filter(100.0) == pytest.approx(math.log(100) / 14, abs=0.01)

    def test_zero_volume(self):
        assert liquidity_filter(0.0) == 0.0
        assert liquidity_filter(None) == 0.0


class TestBayesWinrate:
    def test_zero_samples_returns_prior(self):
        """Beta(5, 3) → 5/8 = 0.625."""
        assert bayes_winrate(0, 0) == pytest.approx(0.625)

    def test_many_wins_shrinks_toward_data(self):
        """20 wins, 0 losses → (25)/(28) ≈ 0.893."""
        assert bayes_winrate(20, 0) == pytest.approx(25 / 28)

    def test_balanced_data_near_prior(self):
        """10/10 → 15/28 = 0.536. Prior pulls slightly above 0.5."""
        assert bayes_winrate(10, 10) == pytest.approx(15 / 28)


class TestMarketRegime:
    def test_bearish_full_strength(self):
        assert market_regime(-0.02) == 1.0

    def test_bullish_penalty(self):
        assert market_regime(0.01) == 0.7

    def test_none_uncertainty(self):
        assert market_regime(None) == 0.85


class TestConcentrationPenalty:
    def test_same_ticker_full_block(self):
        assert concentration_penalty(
            "7203", "auto", {"7203"}, {"auto": 1}
        ) == 1.0

    def test_no_overlap(self):
        assert concentration_penalty(
            "7203", "auto", {"6758"}, {"electronics": 1}
        ) == 0.0

    def test_same_sector_one_position(self):
        assert concentration_penalty(
            "7203", "auto", {"6902"}, {"auto": 1}
        ) == pytest.approx(0.20)

    def test_same_sector_three_positions(self):
        assert concentration_penalty(
            "7203", "auto", set(), {"auto": 3}
        ) == pytest.approx(0.60)

    def test_same_sector_capped(self):
        """5 same-sector → would be 1.0 but capped at 0.80."""
        assert concentration_penalty(
            "7203", "auto", set(), {"auto": 5}
        ) == pytest.approx(0.80)


# ============================================================================
# Cap function tests
# ============================================================================

class TestWeightsLoader:
    def test_default_weights_used_when_no_json(self, tmp_path, monkeypatch):
        """No JSON → DEFAULT_WEIGHTS."""
        monkeypatch.setenv("EVS_WEIGHTS_PATH", str(tmp_path / "missing.json"))
        new = evs_module.reload_weights()
        assert new == DEFAULT_WEIGHTS

    def test_valid_json_overrides_defaults(self, tmp_path, monkeypatch):
        import json
        path = tmp_path / "weights.json"
        path.write_text(json.dumps({
            "weights": {"F1": 0.50, "F2": 0.05, "F3": 0.15, "F4": 0.15, "F5": 0.15},
            "metadata": {"n_samples": 100},
        }))
        monkeypatch.setenv("EVS_WEIGHTS_PATH", str(path))
        new = evs_module.reload_weights()
        assert new["F1"] == 0.50
        assert new["F2"] == 0.05
        # Restore for other tests
        monkeypatch.delenv("EVS_WEIGHTS_PATH", raising=False)
        evs_module.reload_weights()

    def test_invalid_json_falls_back(self, tmp_path, monkeypatch):
        path = tmp_path / "bad.json"
        path.write_text("not valid json{")
        monkeypatch.setenv("EVS_WEIGHTS_PATH", str(path))
        new = evs_module.reload_weights()
        assert new == DEFAULT_WEIGHTS

    def test_missing_key_falls_back(self, tmp_path, monkeypatch):
        import json
        path = tmp_path / "partial.json"
        path.write_text(json.dumps({"weights": {"F1": 0.5}}))  # missing F2-F5
        monkeypatch.setenv("EVS_WEIGHTS_PATH", str(path))
        new = evs_module.reload_weights()
        assert new == DEFAULT_WEIGHTS

    def test_negative_weight_falls_back(self, tmp_path, monkeypatch):
        import json
        path = tmp_path / "neg.json"
        path.write_text(json.dumps({"weights": {"F1": -0.1, "F2": 0.3, "F3": 0.3, "F4": 0.2, "F5": 0.2}}))
        monkeypatch.setenv("EVS_WEIGHTS_PATH", str(path))
        new = evs_module.reload_weights()
        assert new == DEFAULT_WEIGHTS


class TestCapFunction:
    def test_cap_at_zero_evs(self):
        assert cap_function(0.0) == pytest.approx(0.10)

    def test_cap_at_half_evs(self):
        assert cap_function(0.5) == pytest.approx(0.40)

    def test_cap_at_full_evs(self):
        assert cap_function(1.0) == pytest.approx(0.70)

    def test_cap_clipped_above_one(self):
        assert cap_function(1.5) == pytest.approx(0.70)

    def test_cap_clipped_below_zero(self):
        assert cap_function(-0.3) == pytest.approx(0.10)


# ============================================================================
# Integration: compute_evs full pipeline
# ============================================================================

class TestComputeEVSIntegration:
    def _perfect_kwargs(self) -> dict:
        return dict(
            consensus=5, actual_dev=-0.10, sector_threshold=-0.05,
            rsi=18.0, close=980.0, bb_lower=1000.0,
            volumes_3day=(2000.0, 1500.0, 1000.0),  # 50% decline, monotonic
            sector_wins=10, sector_losses=2,
            nikkei_ma25_dev=-0.02,
            realized_vol_20d=0.20,  # 20% vol (normal)
            avg_volume_20d=1_000_000.0,  # high liquidity
            target_ticker="7203", target_sector="auto",
            held_tickers=set(), held_sectors={},
        )

    def test_perfect_setup_high_evs(self):
        """5/5 consensus, deep dev, oversold RSI, deep BB, declining vol, good sector."""
        c = compute_evs(**self._perfect_kwargs())
        assert c.f1_signal_strength == pytest.approx(1.0)
        assert c.f2_deviation_depth == pytest.approx(1.0)  # 10/5=2x = capped
        assert c.f5_volume_decline == pytest.approx(1.0)  # 50% decline = full
        assert c.f6_bayes_winrate == pytest.approx(15 / 20)  # (10+5)/(12+8)
        assert c.f7_market_regime == 1.0
        assert c.f8_realized_vol_filter > 0.5  # 20% vol → 0.6
        assert c.f9_concentration_penalty == 0.0
        assert c.f10_liquidity_filter > 0.9  # log(1M) ≈ 13.8 / 14
        # EVS should be high (≥ 0.4 for "high confidence" in 10-factor regime)
        assert c.evs_total >= 0.40

    def test_weak_setup_low_evs(self):
        """Bare 3/5, just at threshold, weak others, bullish market, high vol, thin liquidity."""
        c = compute_evs(
            consensus=3, actual_dev=-0.05, sector_threshold=-0.05,
            rsi=33.0, close=999.0, bb_lower=1000.0,
            volumes_3day=(1000.0, 1100.0, 1200.0),  # increasing (bad)
            sector_wins=0, sector_losses=0,
            nikkei_ma25_dev=0.02,
            realized_vol_20d=0.45,  # 45% high vol
            avg_volume_20d=5000.0,  # thin liquidity
            target_ticker="7203", target_sector="auto",
            held_tickers=set(), held_sectors={},
        )
        assert c.f5_volume_decline == 0.0  # increasing volume → 0
        assert c.evs_total < 0.20  # multiple penalties stack

    def test_concentration_penalty_drops_evs(self):
        """Perfect signal but already 3 positions in same sector → penalty drops EVS."""
        kwargs = self._perfect_kwargs()
        kwargs.update(
            held_tickers={"6758", "6902", "7011"},
            held_sectors={"auto": 3},
        )
        c = compute_evs(**kwargs)
        assert c.f9_concentration_penalty == pytest.approx(0.60)
        # EVS should drop significantly: perfect_evs × 0.40
        c_perfect = compute_evs(**self._perfect_kwargs())
        assert c.evs_total < c_perfect.evs_total * 0.50


# ============================================================================
# Sizing decisions
# ============================================================================

class TestEvsSize:
    @staticmethod
    def _components(evs: float, cap: float) -> EVSComponents:
        """Helper: bare EVSComponents for sizing tests."""
        return EVSComponents(
            f1_signal_strength=0, f2_deviation_depth=0, f3_rsi_oversold=0,
            f4_bb_penetration=0, f5_volume_decline=0,
            f6_bayes_winrate=0, f7_market_regime=0, f8_realized_vol_filter=0,
            f9_concentration_penalty=0, f10_liquidity_filter=0,
            edge_score=0, prior_adj=0,
            evs_total=evs, cap_pct=cap,
        )

    def test_consensus_below_threshold_zero(self):
        c = self._components(evs=0.5, cap=0.40)
        shares, reason = evs_size(
            components=c, capital=1_000_000, price=1000, flex=True, consensus=2
        )
        assert (shares, reason) == (0, "consensus_low")

    def test_high_evs_p1m_expensive_stock_full_cap(self):
        """EVS=0.95 → cap=67%. p1m ¥1M × 67% = ¥670K, price ¥4K → 100 shares (1 unit)."""
        c = self._components(evs=0.95, cap=0.67)
        shares, reason = evs_size(
            components=c, capital=1_000_000, price=4000, flex=True, consensus=5
        )
        # 670_000 / 4000 / 100 = 1.675 → 100 shares (1 unit, rounded down to 100s)
        assert shares == 100
        assert reason == "evs_cap"

    def test_low_evs_p1m_expensive_stock_flex_min_unit(self):
        """EVS=0.30 → cap=28%. p1m × 28% = ¥280K, price ¥4K → 0 shares from cap,
        but flex=True guarantees 1 unit since cash allows."""
        c = self._components(evs=0.30, cap=0.28)
        shares, reason = evs_size(
            components=c, capital=1_000_000, price=4000, flex=True, consensus=3
        )
        assert shares == 100
        assert reason == "flex_min_unit"

    def test_zero_evs_blocks_even_flex(self):
        """EVS=0 (e.g., concentration overrides edge) → no entry even in flex book."""
        c = self._components(evs=0.0, cap=0.10)
        shares, reason = evs_size(
            components=c, capital=1_000_000, price=1000, flex=True, consensus=4
        )
        assert (shares, reason) == (0, "evs_zero")

    def test_large_book_no_flex_size_scales(self):
        """p50m ¥50M × cap 0.50 = ¥25M. Price ¥3000 → 8333 → 8300 shares."""
        c = self._components(evs=0.65, cap=0.49)
        shares, reason = evs_size(
            components=c, capital=50_000_000, price=3000, flex=False, consensus=4
        )
        # 50M × 0.49 = 24.5M, /3000 = 8166.6, //100*100 = 8100
        assert shares == 8100
        assert reason == "evs_cap"

    def test_p5m_69200_72pct_blocked(self):
        """The bad case from real data: p5m × 69200 (price ¥36049) was 72% concentration.
        Without flex, EVS=0.4 (mediocre signal) → cap=34% → 0 shares (price > cap)."""
        c = self._components(evs=0.40, cap=0.34)
        shares, reason = evs_size(
            components=c, capital=5_000_000, price=36049, flex=True, consensus=3
        )
        # cap_value = 5M × 0.34 = ¥1.7M, /36049 = 47.16 → 0 (100-unit round)
        # flex=True but EVS > 0 → flex_min_unit triggers
        # 1 unit cost = 36049 × 100 = ¥3.6M < ¥5M capital → CAN buy 1 unit
        # **This is the documented Grove tradeoff**: high-price stock in small book.
        # Phase 0 still allows entry as 1-unit (flex_min_unit) but EVS being low
        # means it ranks below other candidates in portfolio selection (Phase 2).
        assert shares == 100  # flex saves it
        assert reason == "flex_min_unit"

    def test_p5m_high_price_low_evs_low_cash_blocked(self):
        """Same as above but EVS very low + capital not enough for 1 unit."""
        c = self._components(evs=0.20, cap=0.22)
        shares, reason = evs_size(
            components=c, capital=2_000_000, price=36049, flex=True, consensus=3
        )
        # 1 unit = ¥3.6M > ¥2M capital → flex cannot save
        assert shares == 0
        assert reason == "shares_zero"
