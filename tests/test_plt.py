"""Tests for src/sizing/plt.py — Pattern Lookup Table.

Phase 0+ (Grove 2026-05-21):
- 432-cell hexa-axis indexing
- Bayesian shrinkage + Fractional Kelly
- DuckDB persistence
- ε-greedy schedule
"""
from __future__ import annotations

import os
import tempfile
from datetime import datetime

import duckdb
import pytest

from src.sizing.plt import (
    CAP_CEILING,
    CAP_FLOOR,
    CellKey,
    CellStats,
    KELLY_FRACTION,
    aggregate_cell,
    assign_bb_bin,
    assign_dev_bin,
    assign_regime,
    assign_rsi_bin,
    assign_sector,
    cap_from_kelly,
    ensure_plt_table,
    exploration_rate,
    features_to_cell,
    kelly_fraction,
    lookup_cell,
    shrunk_win_rate,
    total_samples,
    upsert_cell,
)


# ============================================================================
# CellKey + bin assignment
# ============================================================================

class TestCellKey:
    def test_to_id_format(self):
        key = CellKey(consensus=5, dev_bin=1, rsi_bin=2, bb_bin=0, regime="bear", sector="tech")
        assert key.to_id() == "c5_d1_r2_b0_bear_tech"

    def test_round_trip(self):
        key = CellKey(consensus=4, dev_bin=0, rsi_bin=1, bb_bin=1, regime="bull", sector="pharma")
        parsed = CellKey.from_id(key.to_id())
        assert parsed == key

    def test_from_id_invalid_format(self):
        with pytest.raises(ValueError):
            CellKey.from_id("invalid_id")


class TestBinAssignment:
    def test_dev_bin_split_at_half(self):
        assert assign_dev_bin(0.3) == 0
        assert assign_dev_bin(0.5) == 1
        assert assign_dev_bin(0.9) == 1

    def test_rsi_bin_thresholds(self):
        assert assign_rsi_bin(10.0) == 0
        assert assign_rsi_bin(15.0) == 1  # boundary
        assert assign_rsi_bin(20.0) == 1
        assert assign_rsi_bin(25.0) == 2
        assert assign_rsi_bin(40.0) == 2  # above RSI<35 gate (defensive)

    def test_bb_bin_thresholds(self):
        assert assign_bb_bin(0.0) == 0
        assert assign_bb_bin(0.3) == 1
        assert assign_bb_bin(1.0) == 1

    def test_regime_bear_bull(self):
        assert assign_regime(-0.01) == "bear"
        assert assign_regime(0.0) == "bull"
        assert assign_regime(0.05) == "bull"
        assert assign_regime(None) == "bull"  # conservative default

    def test_sector_known(self):
        assert assign_sector("4502") == "pharma"
        assert assign_sector("6758") == "tech"

    def test_sector_invalid_input_falls_to_other(self):
        """無効入力 (空文字/4桁未満/非数値) は'other'."""
        assert assign_sector("") == "other"
        assert assign_sector("abc") == "other"
        assert assign_sector("12") == "other"

    def test_sector_pattern_matching_5digit(self):
        """2026-05-25 拡張: 4桁前綴 pattern matching でTICKER_SECTORS辞書
        外のtickerも分類。1351銘柄ユニバースで F9 concentration が機能するため。"""
        # food
        assert assign_sector("15010") == "food"  # 水産農林
        assert assign_sector("22090") == "food"  # 加工食品
        assert assign_sector("29310") == "food"  # ユーグレナ (明示辞書既存)
        # const
        assert assign_sector("19250") == "const"  # 大和ハウス
        assert assign_sector("18000") == "const"
        # chem
        assert assign_sector("42200") == "chem"
        assert assign_sector("48390") == "chem"
        assert assign_sector("39000") == "chem"
        # tech
        assert assign_sector("63630") == "tech"
        assert assign_sector("79740") == "tech"  # 任天堂
        assert assign_sector("99840") == "tech"  # SBG (明示辞書既存)
        # sec
        assert assign_sector("85930") == "sec"
        assert assign_sector("83060") == "sec"  # 三菱UFJ (明示辞書既存)
        # other (自動車/小売/運輸)
        assert assign_sector("70030") == "other"
        assert assign_sector("70130") == "other"
        assert assign_sector("82670") == "other"  # イオン
        assert assign_sector("90200") == "other"  # JR東日本

    def test_sector_explicit_overrides_pattern(self):
        """明示辞書TICKER_SECTORSが pattern matching より優先される
        (戦略核心のpharma/食品閾値が変わらないことを保証)."""
        # 45020 → 明示辞書で pharma (pattern なら 4500 = chem)
        assert assign_sector("45020") == "pharma"
        # 5-digit pharma が pattern (chem) より優先
        assert assign_sector("45190") == "pharma"


class TestFeaturesToCell:
    def test_full_mapping(self):
        cell = features_to_cell(
            consensus=5, dev_depth_score=0.75, rsi=18.0,
            bb_pen_score=0.5, nikkei_ma25_dev=-0.02, ticker="6758",
        )
        assert cell.consensus == 5
        assert cell.dev_bin == 1
        assert cell.rsi_bin == 1
        assert cell.bb_bin == 1
        assert cell.regime == "bear"
        assert cell.sector == "tech"
        assert cell.to_id() == "c5_d1_r1_b1_bear_tech"

    def test_clips_consensus_to_valid_range(self):
        cell = features_to_cell(
            consensus=2, dev_depth_score=0.5, rsi=20.0,
            bb_pen_score=0.5, nikkei_ma25_dev=None, ticker="4502",
        )
        assert cell.consensus == 3  # clipped


# ============================================================================
# Empirical Bayes math
# ============================================================================

class TestShrunkWinRate:
    def test_zero_samples_prior(self):
        """Beta(5,3) → 5/8 = 0.625."""
        assert shrunk_win_rate(0, 0) == pytest.approx(0.625)

    def test_consistent_with_evs_bayes(self):
        """Same prior as evs.bayes_winrate."""
        from src.sizing.evs import bayes_winrate
        for w, l in [(0, 0), (5, 5), (10, 2), (3, 8)]:
            assert shrunk_win_rate(w, l) == bayes_winrate(w, l)


class TestKellyFraction:
    def test_zero_win_rate(self):
        assert kelly_fraction(0.0, 0.03, -0.05) == 0.0

    def test_breakeven_no_edge(self):
        """p=0.5, b=1.0 → f = (0.5 - 0.5)/1 = 0."""
        assert kelly_fraction(0.50, 0.05, -0.05) == 0.0

    def test_positive_edge(self):
        """p=0.7, b=2.0 → f = (1.4 - 0.3)/2 = 0.55."""
        assert kelly_fraction(0.70, 0.04, -0.02) == pytest.approx(0.55)

    def test_extreme_edge_capped(self):
        """p=0.95, b=5.0 → f = (4.75 - 0.05)/5 = 0.94. Still capped at 1."""
        assert kelly_fraction(0.95, 0.10, -0.02) == pytest.approx(0.94, abs=0.01)

    def test_negative_avg_win_falls_back(self):
        """Insufficient data — uses DEFAULT_B_RATIO."""
        # win_rate=0.7, b=0.6 default → (0.7*0.6 - 0.3)/0.6 = 0.20
        assert kelly_fraction(0.70, 0.0, 0.0) == pytest.approx(0.20)


class TestCapFromKelly:
    def test_quarter_kelly(self):
        """f*=0.8 → cap = 0.8 × 0.25 = 0.20."""
        assert cap_from_kelly(0.80) == pytest.approx(0.20)

    def test_cap_floor_applied(self):
        """f*=0 → cap = floor (10%)."""
        assert cap_from_kelly(0.0) == CAP_FLOOR

    def test_cap_ceiling_applied(self):
        """f*=10.0 (impossible but tested) → cap = ceiling (70%)."""
        assert cap_from_kelly(10.0) == CAP_CEILING


# ============================================================================
# aggregate_cell
# ============================================================================

class TestAggregateCell:
    def test_empty_cell_cold(self):
        s = aggregate_cell("c3_d0_r2_b0_bull_other", [])
        assert s.n_samples == 0
        assert s.confidence == "cold"
        assert s.recommended_cap_pct == CAP_FLOOR
        assert s.shrunk_win_rate == pytest.approx(0.625)

    def test_few_samples_cold(self):
        """4 samples, all wins → still cold (<5)."""
        s = aggregate_cell("test_id", [0.03, 0.02, 0.05, 0.04])
        assert s.confidence == "cold"
        assert s.n_samples == 4
        assert s.n_wins == 4

    def test_warm_threshold(self):
        """5 samples → warm."""
        s = aggregate_cell("test_id", [0.03, 0.02, 0.05, 0.04, -0.05])
        assert s.confidence == "warm"

    def test_hot_threshold(self):
        """20+ samples → hot."""
        pnls = [0.02] * 14 + [-0.05] * 6
        s = aggregate_cell("test_id", pnls)
        assert s.confidence == "hot"
        assert s.n_samples == 20
        assert s.n_wins == 14

    def test_shrinkage_with_data(self):
        """10 wins / 2 losses → shrunk = (10+5)/(12+8) = 0.75."""
        pnls = [0.02] * 10 + [-0.05] * 2
        s = aggregate_cell("test_id", pnls)
        assert s.shrunk_win_rate == pytest.approx(0.75)

    def test_avg_win_loss_separated(self):
        s = aggregate_cell("test_id", [0.03, 0.04, -0.05, -0.06])
        assert s.avg_win_pct == pytest.approx(0.035)
        assert s.avg_loss_pct == pytest.approx(-0.055)

    def test_kelly_computed_from_shrunk_winrate(self):
        """High win rate → positive Kelly → cap > floor."""
        pnls = [0.03] * 15 + [-0.05] * 3  # 18 samples, hot
        s = aggregate_cell("test_id", pnls)
        assert s.kelly_fraction > 0
        # winrate shrunk = (15+5)/(18+8) = 0.769
        # b = 0.03/0.05 = 0.6
        # f = (0.769*0.6 - 0.231)/0.6 = 0.385
        # cap = 0.385 * 0.25 = 0.096 → floor 0.10
        assert s.recommended_cap_pct >= CAP_FLOOR


# ============================================================================
# Exploration schedule
# ============================================================================

class TestExplorationRate:
    def test_high_when_sparse(self):
        assert exploration_rate(0) == 0.20
        assert exploration_rate(49) == 0.20

    def test_low_when_sufficient(self):
        assert exploration_rate(50) == 0.10
        assert exploration_rate(500) == 0.10


# ============================================================================
# DuckDB persistence
# ============================================================================

class TestPersistence:
    @pytest.fixture
    def con(self):
        """In-memory DuckDB connection."""
        c = duckdb.connect(":memory:")
        yield c
        c.close()

    def test_ensure_table_idempotent(self, con):
        ensure_plt_table(con)
        ensure_plt_table(con)  # second call should not error
        result = con.execute("SELECT count(*) FROM plt_cells").fetchone()
        assert result[0] == 0

    def test_upsert_and_lookup(self, con):
        stats = CellStats(
            cell_id="test_cell",
            n_samples=10, n_wins=7,
            avg_pnl_pct=0.012, std_pnl_pct=0.03,
            avg_win_pct=0.025, avg_loss_pct=-0.04,
            shrunk_win_rate=0.667, kelly_fraction=0.30,
            recommended_cap_pct=0.30,
            last_updated=datetime(2026, 5, 21, 12, 0, 0),
            confidence="warm",
        )
        upsert_cell(con, stats)
        retrieved = lookup_cell(con, "test_cell")
        assert retrieved is not None
        assert retrieved.n_samples == 10
        assert retrieved.shrunk_win_rate == pytest.approx(0.667)

    def test_upsert_idempotent(self, con):
        """Upsert same cell twice → still one row."""
        s = aggregate_cell("dup_cell", [0.02, 0.03, -0.05])
        upsert_cell(con, s)
        upsert_cell(con, s)
        result = con.execute(
            "SELECT count(*) FROM plt_cells WHERE cell_id = 'dup_cell'"
        ).fetchone()
        assert result[0] == 1

    def test_upsert_update_overwrites(self, con):
        """Second upsert with new n_samples overwrites."""
        s1 = aggregate_cell("u_cell", [0.02])
        upsert_cell(con, s1)
        s2 = aggregate_cell("u_cell", [0.02, 0.03, 0.04])
        upsert_cell(con, s2)
        retrieved = lookup_cell(con, "u_cell")
        assert retrieved is not None
        assert retrieved.n_samples == 3

    def test_lookup_missing_returns_none(self, con):
        ensure_plt_table(con)
        assert lookup_cell(con, "nonexistent") is None

    def test_total_samples_sums(self, con):
        upsert_cell(con, aggregate_cell("c1", [0.02] * 3))
        upsert_cell(con, aggregate_cell("c2", [0.03] * 5))
        upsert_cell(con, aggregate_cell("c3", [-0.05] * 7))
        assert total_samples(con) == 15
