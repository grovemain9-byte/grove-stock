"""Phase A Boyd continuous drawdown cushion テスト (2026-05-27).

frontier math (Stanford Boyd + arXiv 1710.01503) を grove-stock に実装。
本田くん哲学「動的にプラス、決まった日数や%で固めない」を smooth 関数で実現。

検証項目:
- compute_boyd_cushion 数値 (DD 0%, -2%, -5%, -7.5%, -10%)
- HWM ratchet (新 high で update)
- book pause (cushion < threshold で book skip)
"""
from __future__ import annotations

import pytest

from src.data.db import (
    compute_boyd_cushion,
    get_book_hwm,
    get_connection,
    update_book_hwm,
)


class TestBoydCushion:
    """Boyd continuous formula の数値挙動."""

    def test_cushion_at_hwm_full_size(self):
        """equity == hwm → cushion = 1.0 (full size)."""
        c = compute_boyd_cushion(equity=100.0, hwm=100.0)
        assert c == 1.0

    def test_cushion_above_hwm_capped_at_one(self):
        """equity > hwm → cushion = 1.0 (HWM ratchet 別途実行で normal化)."""
        c = compute_boyd_cushion(equity=105.0, hwm=100.0)
        assert c == 1.0

    def test_cushion_at_minus_2pct(self):
        """DD = -2% → cushion ≈ 0.733 (1 + (-0.02/-0.075)^1)."""
        c = compute_boyd_cushion(equity=98.0, hwm=100.0)
        assert abs(c - 0.7333) < 0.001

    def test_cushion_at_minus_5pct(self):
        """DD = -5% → cushion ≈ 0.333 (慎重モード)."""
        c = compute_boyd_cushion(equity=95.0, hwm=100.0)
        assert abs(c - 0.3333) < 0.001

    def test_cushion_at_hard_limit(self):
        """DD = -7.5% → cushion = 0.0 (book pause)."""
        c = compute_boyd_cushion(equity=92.5, hwm=100.0)
        assert c == 0.0

    def test_cushion_below_hard_limit(self):
        """DD < -7.5% → cushion = 0.0 (book pause 継続)."""
        c = compute_boyd_cushion(equity=85.0, hwm=100.0)  # -15%
        assert c == 0.0

    def test_cushion_with_alpha_quadratic(self):
        """alpha=2.0 → quadratic decay (より急速縮小)."""
        # DD = -5% で alpha=2 → (1 + (-0.05/-0.075))^2 = (0.333)^2 ≈ 0.111
        c = compute_boyd_cushion(equity=95.0, hwm=100.0, alpha=2.0)
        assert abs(c - 0.1111) < 0.001

    def test_cushion_custom_hard_limit(self):
        """dd_hard_limit を緩める例 (-10% で pause)."""
        # DD = -7.5% で dd_hard_limit=-0.10 → (1 - 0.075/0.10)^1 = 0.25
        c = compute_boyd_cushion(equity=92.5, hwm=100.0, dd_hard_limit=-0.10)
        assert abs(c - 0.25) < 0.001

    def test_cushion_zero_hwm_safety(self):
        """HWM未初期化 (0) → cushion = 1.0 (safety: full size)."""
        c = compute_boyd_cushion(equity=100.0, hwm=0.0)
        assert c == 1.0


class TestHwmTracking:
    """book HWM persistence (DB-based)."""

    def test_initial_hwm_seeded_from_initial_capital(self, tmp_path):
        """初回 get_book_hwm: book_hwm に initial_capital で seed される."""
        db = str(tmp_path / "hwm_test.db")
        con = get_connection(db)
        hwm = get_book_hwm(con, "p1m", initial_capital=1_000_000.0)
        assert hwm == 1_000_000.0

        # 再呼出で同 value (idempotent)
        hwm2 = get_book_hwm(con, "p1m", initial_capital=2_000_000.0)
        assert hwm2 == 1_000_000.0  # 既登録故 initial_capital arg 無視
        con.close()

    def test_hwm_ratchet_updates_on_new_high(self, tmp_path):
        """update_book_hwm: equity > HWM なら更新、戻り値が新 HWM."""
        db = str(tmp_path / "ratchet_test.db")
        con = get_connection(db)
        # Init at 1M
        get_book_hwm(con, "p1m", 1_000_000.0)
        # Update to 1.05M (新 high)
        new_hwm = update_book_hwm(con, "p1m", 1_050_000.0)
        assert new_hwm == 1_050_000.0
        # Persistence確認
        hwm_stored = get_book_hwm(con, "p1m", 0.0)
        assert hwm_stored == 1_050_000.0
        con.close()

    def test_hwm_no_update_when_equity_lower(self, tmp_path):
        """equity < HWM → HWM 不変 (ratchet は一方向)."""
        db = str(tmp_path / "noupdate_test.db")
        con = get_connection(db)
        get_book_hwm(con, "p1m", 1_000_000.0)
        update_book_hwm(con, "p1m", 1_100_000.0)  # HWM = 1.1M
        # equity 下落
        result = update_book_hwm(con, "p1m", 1_080_000.0)
        assert result == 1_100_000.0  # HWM 維持
        con.close()


class TestBookPauseLogic:
    """cushion < threshold で book pause シナリオ (integration spec)."""

    def test_cushion_threshold_for_pause(self):
        """CUSHION_PAUSE_THRESHOLD=0.05 で book skip判定."""
        from config.strategy_params import CUSHION_PAUSE_THRESHOLD
        assert CUSHION_PAUSE_THRESHOLD == 0.05

        # DD -7% (ハード限界手前) → cushion = (1 - 0.07/0.075)^1 ≈ 0.067 > 0.05、まだ稼働
        c_near_limit = compute_boyd_cushion(equity=93.0, hwm=100.0)
        assert c_near_limit > CUSHION_PAUSE_THRESHOLD

        # DD -7.4% → cushion ≈ 0.013 < 0.05 → pause
        c_paused = compute_boyd_cushion(equity=92.6, hwm=100.0)
        assert c_paused < CUSHION_PAUSE_THRESHOLD


class TestConfigConstants:
    """Phase A constants の存在確認."""

    def test_dd_hard_limit_value(self):
        from config.strategy_params import DD_HARD_LIMIT
        assert DD_HARD_LIMIT == -0.075

    def test_cushion_alpha_value(self):
        from config.strategy_params import CUSHION_ALPHA
        assert CUSHION_ALPHA == 1.0

    def test_cushion_pause_threshold_value(self):
        from config.strategy_params import CUSHION_PAUSE_THRESHOLD
        assert CUSHION_PAUSE_THRESHOLD == 0.05
