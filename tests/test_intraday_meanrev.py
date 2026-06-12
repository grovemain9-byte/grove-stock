"""test_intraday_meanrev.py — scripts/intraday_meanrev.py の純粋ロジック unit-test.

口座なしで回る (API 不要)。2 系統:

  [A] 合成データでの単体テスト
      - dip 検出 / near-close 判定 / exit / Tiered Kelly サイジング / kill-criterion
      - 境界値・冪等性も含む

  [B] HISTORICAL データでの再現テスト (本タスクの核心)
      data/grove_stock.duckdb (positions) + data/history_cache.duckdb (daily_quotes):
      B1. dip-detector が signal_reversal entry set を「概ね」再現するか
          - 全 entry が MA25 dip 側 (deviation<0) にあること
          - per-sector kσ 閾値で >=60% 再現、-3% ceiling で >=78% 再現
      B2. near-close exec + sizing が close-exec economics を再現するか
          - close_exec_economics が close_exec_test.py の数値を再現
            (gross +¥2.7488M, net@10bps ≈ +¥1.66M, breakeven 35.1bps/side)
          - kill-criterion の閾値 35bps が breakeven と一致

実行: PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 pytest tests/test_intraday_meanrev.py -v
"""
from __future__ import annotations

import os
from datetime import time as dt_time

import pytest

from scripts.intraday_meanrev import (
    DEFAULT_SECTOR_TH,
    KILL_PER_SIDE_BPS,
    Leg,
    breakeven_timing_bps,
    close_exec_economics,
    detect_dip,
    kelly_fraction_full,
    kill_triggered,
    ma25_deviation,
    realized_cost_bps_per_side,
    reverts_to_ma25,
    should_exit,
    should_fire_near_close,
    tier_for_edge,
    tiered_kelly_size,
)

POS_DB = "/Users/ryu/grove-stock/data/grove_stock.duckdb"
HIST_DB = "/Users/ryu/grove-stock/data/history_cache.duckdb"

# 凍結スナップショット境界。paper pipeline は今も毎日 close を積むため、上限なしの
# 母集団クエリは日々ドリフトする (観測: 06-09まで=326件/gross¥2,748,800 ちょうど、
# 06-10で+3件、06-11で+4件→333件/¥2,841,550)。[B] の anchor (326 / ¥2.7488M /
# breakeven 35.1bps) は close_exec_test.py が 2026-06-09 時点母集団で測った値の再現
# テストであり、エッジの証明ではない (価格系譜は P5 検証で汚染と判明済み)。
SNAPSHOT_CLOSE_DATE = "2026-06-09"


# ===========================================================================
# [A] 合成データ — 単体テスト
# ===========================================================================

class TestMa25Deviation:
    def test_below_ma_is_negative(self):
        series = [1000.0] * 25 + [900.0]
        dev = ma25_deviation(series)
        # MA25 of last 25 = (1000*24 + 900)/25 = 996.0; dev=(900-996)/996
        assert dev is not None
        assert dev < 0
        assert dev == pytest.approx((900 - 996.0) / 996.0)

    def test_insufficient_data_returns_none(self):
        assert ma25_deviation([1000.0] * 10) is None

    def test_flat_series_zero_deviation(self):
        assert ma25_deviation([1000.0] * 30) == pytest.approx(0.0)


class TestDetectDip:
    def test_dip_below_threshold(self):
        # last close 8% below a flat 1000 base → dev ≈ -8%
        series = [1000.0] * 25 + [920.0]
        assert detect_dip(series, sector_threshold=-0.07) is True

    def test_no_dip_above_threshold(self):
        series = [1000.0] * 25 + [990.0]  # dev ≈ -1%, not <= -7%
        assert detect_dip(series, sector_threshold=-0.07) is False

    def test_boundary_exactly_at_threshold_is_dip(self):
        # construct so dev == threshold exactly: dev<=th must be inclusive
        base = [1000.0] * 24
        # choose last two so MA25 and last give a clean -5%
        series = base + [1000.0, 950.0]
        ma = sum(series[-25:]) / 25
        dev = (950.0 - ma) / ma
        assert detect_dip(series, sector_threshold=dev) is True  # inclusive

    def test_insufficient_data_fail_safe_false(self):
        assert detect_dip([1000.0] * 5, -0.07) is False


class TestNearClose:
    def test_within_5min_window(self):
        assert should_fire_near_close(dt_time(15, 26)) is True
        assert should_fire_near_close(dt_time(15, 25)) is True  # window start inclusive

    def test_before_window(self):
        assert should_fire_near_close(dt_time(15, 20)) is False
        assert should_fire_near_close(dt_time(12, 0)) is False

    def test_at_or_after_close_excluded(self):
        assert should_fire_near_close(dt_time(15, 30)) is False  # close excluded
        assert should_fire_near_close(dt_time(15, 35)) is False

    def test_custom_window(self):
        assert should_fire_near_close(dt_time(15, 20), window_min=15) is True


class TestExit:
    def test_take_profit_on_reversion(self):
        # last close >= +2% above MA25 → take_profit
        series = [1000.0] * 24 + [1000.0, 1040.0]
        dev = ma25_deviation(series)
        assert dev is not None and dev >= 0.02
        ex, reason = should_exit(series, entry_price=950.0, days_held=1)
        assert ex is True and reason == "take_profit"

    def test_stop_loss(self):
        # below MA so no TP; price 8% below entry → stop_loss
        series = [1000.0] * 25 + [920.0]
        ex, reason = should_exit(series, entry_price=1000.0, days_held=1)
        assert ex is True and reason == "stop_loss"

    def test_max_hold(self):
        # mild dip: no TP (dev<2%), no stop (ret>-7%), but held 15 days
        series = [1000.0] * 25 + [990.0]
        ex, reason = should_exit(series, entry_price=1000.0, days_held=15)
        assert ex is True and reason == "max_hold"

    def test_hold(self):
        series = [1000.0] * 25 + [990.0]
        ex, reason = should_exit(series, entry_price=1000.0, days_held=2)
        assert ex is False and reason == "hold"

    def test_reverts_to_ma25(self):
        assert reverts_to_ma25([1000.0] * 25 + [1010.0], dev_threshold=0.0) is True
        assert reverts_to_ma25([1000.0] * 25 + [950.0], dev_threshold=0.0) is False


class TestTieredKelly:
    """~/.claude/rules/trading-rules.md 準拠の検証。Pure Kelly でないこと。"""

    def test_edge_below_10pct_skipped(self):
        # win 0.51 / wl 1.0 → f* = (1*0.51-0.49)/1 = 0.02 < 0.10 → skip
        r = tiered_kelly_size(bankroll=2_000_000, price=1000.0,
                              win_prob=0.51, win_loss_ratio=1.0)
        assert r.shares == 0
        assert r.binding_constraint == "edge_skip"

    def test_pool_cap_binds_at_half_pct(self):
        # large edge → would exceed 0.5% pool cap → capped at 0.5% of 2M = ¥10,000
        r = tiered_kelly_size(bankroll=2_000_000, price=100.0,
                              win_prob=0.80, win_loss_ratio=3.0)
        assert r.binding_constraint == "pool_cap"
        assert r.notional <= 2_000_000 * 0.0050 + 1e-6
        # shares rounded to lot, notional close to but <= ¥10,000
        assert r.shares % 100 == 0

    def test_adv_cap_binds(self):
        # tiny ADV forces adv cap below pool-cap shares
        # pool cap @ price 100 = ¥10,000 → 100 shares. ADV 500 → 10% = 50 < 100 → lot 0?
        # use ADV 1500 → 10%=150 → lot 100. price 50: pool cap ¥10k=200sh, adv 150 binds
        r = tiered_kelly_size(bankroll=2_000_000, price=50.0,
                              win_prob=0.80, win_loss_ratio=3.0, adv_shares=1500)
        assert r.binding_constraint == "adv_cap"
        assert r.shares <= int(0.10 * 1500)

    def test_not_pure_kelly_fraction_applied(self):
        # full kelly fraction must be strictly larger than applied fraction (Tier divides)
        f_full = kelly_fraction_full(0.80, 3.0)
        r = tiered_kelly_size(bankroll=2_000_000, price=100.0,
                              win_prob=0.80, win_loss_ratio=3.0)
        # applied (post-cap) must never exceed full kelly and must respect 0.5% cap
        assert r.kelly_fraction < f_full
        assert r.kelly_fraction <= 0.0050 + 1e-9

    def test_tier_thresholds(self):
        assert tier_for_edge(0.05) == 0
        assert tier_for_edge(0.10) == 1
        assert tier_for_edge(0.20) == 2
        assert tier_for_edge(0.35) == 3
        assert tier_for_edge(0.90) == 3

    def test_zero_price_invalid(self):
        r = tiered_kelly_size(bankroll=2_000_000, price=0.0,
                              win_prob=0.80, win_loss_ratio=3.0)
        assert r.shares == 0

    def test_shares_are_lot_rounded(self):
        r = tiered_kelly_size(bankroll=2_000_000, price=137.0,
                              win_prob=0.65, win_loss_ratio=1.5, adv_shares=10_000_000)
        assert r.shares % 100 == 0

    def test_idempotent(self):
        # 冪等: 同入力は同出力 (純粋関数)
        kw = dict(bankroll=2_000_000, price=1234.0, win_prob=0.6,
                  win_loss_ratio=1.4, adv_shares=800_000)
        a = tiered_kelly_size(**kw)
        b = tiered_kelly_size(**kw)
        assert a == b


class TestKillCriterion:
    def test_threshold_is_35bps(self):
        assert KILL_PER_SIDE_BPS == 35.0

    def test_kill_triggers_above_threshold(self):
        assert kill_triggered(36.0) is True
        assert kill_triggered(35.0) is False  # strictly greater
        assert kill_triggered(10.0) is False

    def test_realized_cost_components(self):
        # exec exactly at reference → cost = half_spread + commission only
        c = realized_cost_bps_per_side(exec_price=2000.0, reference_price=2000.0)
        # half_spread @2000: tick=1, 0.5*1/2000*1e4 = 2.5 → max(3,2.5)=3.0; +1.5 comm
        assert c == pytest.approx(3.0 + 1.5)

    def test_realized_cost_with_drift(self):
        # 50bps drift + spread + commission
        c = realized_cost_bps_per_side(exec_price=2010.0, reference_price=2000.0)
        assert c == pytest.approx(50.0 + 3.0 + 1.5)
        assert kill_triggered(c) is True  # 54.5 > 35


# ===========================================================================
# [B] HISTORICAL 再現テスト — DB が無ければ skip
# ===========================================================================

def _duckdb_or_skip():
    try:
        import duckdb  # noqa: PLC0415
    except ImportError:
        pytest.skip("duckdb not installed")
    if not (os.path.exists(POS_DB) and os.path.exists(HIST_DB)):
        pytest.skip("historical duckdb files not present")
    return duckdb


@pytest.fixture(scope="module")
def signal_trades():
    """positions の signal_reversal trades (2026-05-25 〜 SNAPSHOT_CLOSE_DATE 凍結)。"""
    duckdb = _duckdb_or_skip()
    con = duckdb.connect(POS_DB, read_only=True)
    rows = con.execute(
        """
        SELECT id, ticker, entry_price, shares, entry_date,
               exit_price, exit_reason, pnl
        FROM positions
        WHERE exit_reason='signal_reversal' AND entry_date >= DATE '2026-05-25'
          AND CAST(closed_at AS DATE) <= CAST(? AS DATE)
        ORDER BY entry_date, ticker, id
        """,
        [SNAPSHOT_CLOSE_DATE],
    ).fetchall()
    cols = [d[0] for d in con.description]
    con.close()
    return [dict(zip(cols, r)) for r in rows]


@pytest.fixture(scope="module")
def hist_con():
    duckdb = _duckdb_or_skip()
    con = duckdb.connect(HIST_DB, read_only=True)
    yield con
    con.close()


class TestHistoricalDipReproduction:
    """B1: dip-detector が signal_reversal entry set を概ね再現するか。"""

    def test_trade_count_anchor(self, signal_trades):
        # close_exec_test.py が 326 trades を assert している (同じ母集団)
        assert len(signal_trades) == 326

    def test_all_entries_on_dip_side(self, signal_trades, hist_con):
        """全 entry が MA25 dip 側 (deviation<0) にあること = 方向の完全再現。"""
        seen: set[tuple[str, object]] = set()
        computable = 0
        on_dip_side = 0
        for t in signal_trades:
            key = (t["ticker"], t["entry_date"])
            if key in seen:
                continue
            seen.add(key)
            closes = [
                r[0] for r in hist_con.execute(
                    "SELECT close FROM daily_quotes WHERE code=? AND date<=? ORDER BY date",
                    [t["ticker"], t["entry_date"]],
                ).fetchall()
            ]
            if len(closes) < 25:
                continue
            dev = ma25_deviation(closes)
            if dev is None:
                continue
            computable += 1
            if dev < 0:
                on_dip_side += 1
        assert computable >= 90, f"too few computable: {computable}"
        # 観測: 97/97 = 100% が dip 側。回帰防止に >=95% を要求。
        frac = on_dip_side / computable
        assert frac >= 0.95, f"only {frac:.1%} on dip side (expected ~100%)"

    def test_dip_detector_reproduces_majority_with_sector_threshold(
        self, signal_trades, hist_con
    ):
        """per-sector kσ 閾値で entry set の過半 (>=60%) を再現。

        100% にならない理由 (honest): ①閾値は日次再計算で point-in-time でない
        (読んでいる snapshot は最新日)、②本番 entry は 3/5 consensus で P1 dip は
        必要条件であって十分条件でない。→「概ね再現」を >=60% で機械的に確認。
        """
        # code -> sector -> threshold
        uni = {
            r[0]: r[1]
            for r in hist_con.execute(
                "SELECT code, s33_name FROM universe_snapshot"
            ).fetchall()
        }
        secth = {
            r[0]: r[1]
            for r in hist_con.execute(
                "SELECT sector, threshold FROM sector_thresholds"
            ).fetchall()
        }
        seen: set[tuple[str, object]] = set()
        computable = 0
        reproduced = 0
        for t in signal_trades:
            key = (t["ticker"], t["entry_date"])
            if key in seen:
                continue
            seen.add(key)
            closes = [
                r[0] for r in hist_con.execute(
                    "SELECT close FROM daily_quotes WHERE code=? AND date<=? ORDER BY date",
                    [t["ticker"], t["entry_date"]],
                ).fetchall()
            ]
            if len(closes) < 25:
                continue
            computable += 1
            sector = uni.get(t["ticker"])
            th = secth.get(sector, DEFAULT_SECTOR_TH)
            if detect_dip(closes, th):
                reproduced += 1
        frac = reproduced / computable
        # 観測 2026-06: 63/97 = 64.9%。回帰防止に下限 0.60。
        assert frac >= 0.60, f"sector-threshold reproduction {frac:.1%} < 60%"

    def test_dip_detector_reproduces_most_with_ceiling_threshold(
        self, signal_trades, hist_con
    ):
        """th_ceiling(-3%) を緩い proxy にすると entry set の大半 (>=78%) を再現。"""
        seen: set[tuple[str, object]] = set()
        computable = 0
        reproduced = 0
        for t in signal_trades:
            key = (t["ticker"], t["entry_date"])
            if key in seen:
                continue
            seen.add(key)
            closes = [
                r[0] for r in hist_con.execute(
                    "SELECT close FROM daily_quotes WHERE code=? AND date<=? ORDER BY date",
                    [t["ticker"], t["entry_date"]],
                ).fetchall()
            ]
            if len(closes) < 25:
                continue
            computable += 1
            if detect_dip(closes, sector_threshold=-0.03):
                reproduced += 1
        frac = reproduced / computable
        # 観測: 78/97 = 80.4%。下限 0.78。
        assert frac >= 0.78, f"ceiling reproduction {frac:.1%} < 78%"


class TestHistoricalCloseExecEconomics:
    """B2: near-close exec + sizing が close-exec economics を再現するか。

    close_exec_test.py の anchor:
      gross           = +¥2,748,800
      net @10bps      ≈ +¥1,655,206  (~+¥1.66M, タスク要求値)
      breakeven       = 35.1 bps/side
    """

    def test_economics_reproduce_close_exec_test(self, signal_trades):
        legs = [
            (Leg(t["entry_price"], t["shares"]),
             Leg(t["exit_price"], t["shares"]),
             t["pnl"])
            for t in signal_trades
        ]
        econ = close_exec_economics(legs)

        # gross = SUM(pnl) booked (再評価しない)
        assert econ.gross == pytest.approx(2_748_800.0, rel=1e-4)

        # net @10bps/side ≈ +¥1.66M (タスクの要求 anchor)
        net10 = econ.net(10.0)
        assert net10 == pytest.approx(1_655_206.0, rel=2e-3), f"net@10bps={net10:,.0f}"
        assert net10 > 1_600_000, "net@10bps should be ~+¥1.66M"

        # net @5bps positive, @20bps still positive (close_exec_test の verdict ALIVE)
        assert econ.net(5.0) > 0
        assert econ.net(20.0) > 0

    def test_breakeven_matches_kill_criterion(self, signal_trades):
        """breakeven timing penalty ≈ 35bps/side = pre-registered kill threshold。"""
        legs = [
            (Leg(t["entry_price"], t["shares"]),
             Leg(t["exit_price"], t["shares"]),
             t["pnl"])
            for t in signal_trades
        ]
        econ = close_exec_economics(legs)
        be = breakeven_timing_bps(econ)
        # close_exec_test.py: 35.1 bps/side
        assert be == pytest.approx(35.1, abs=0.3), f"breakeven={be:.2f}bps"
        # kill-criterion (35bps) は breakeven の床にいる: per-side コストが be を超えたら停止
        assert abs(be - KILL_PER_SIDE_BPS) < 1.0

    def test_kill_criterion_consistent_with_breakeven(self, signal_trades):
        """実現 per-side コストが kill 閾値を超える状況では net<0 になる方向性。"""
        legs = [
            (Leg(t["entry_price"], t["shares"]),
             Leg(t["exit_price"], t["shares"]),
             t["pnl"])
            for t in signal_trades
        ]
        econ = close_exec_economics(legs)
        # timing penalty を kill 閾値(35bps)より上に置くと net は breakeven 近傍で負へ
        assert econ.net(KILL_PER_SIDE_BPS + 5.0) < econ.net(10.0)
