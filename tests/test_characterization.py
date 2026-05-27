"""S0 特性テスト: 現コード挙動を golden 凍結。

目的（plan S0）: S1/S2/S3 の config 集約リファクタが挙動を1ビットも変えない
ことを機械的に証明する土台。ここで固定した golden が S2/S3 後も緑のままなら
「リファクタは挙動保存」、S6(Lane B kσ) でのみ意図的に更新する。

golden 値は決定論入力で現コードを1回実行して捕捉済（2026-05-16）。
docでなく**コードの実挙動**を正とする（CLAUDE.md「コードとテストが今の仕様」）。

F1 反映: live P1 のセクター閾値は ticker=5桁を渡すため sector_config の
4桁キーmap(13銘柄)に当たらず常に DEFAULT -0.07（13銘柄mapは死にコード）。
"""
from __future__ import annotations

import asyncio

import duckdb
import pandas as pd
import pytest

from types import SimpleNamespace

from src.kelly import kelly_size
from src.backtest.engine import _compute_indicators
from src.voting import vote_all
from src.data.db import _create_tables
from config.sector_config import get_threshold
from src.players import p01 as p01_mod


# 決定論的入力（golden 捕捉時と同一。変更厳禁）
_CLOSES = [1000 - i * 8 for i in range(35)] + [720, 718, 716, 714, 712]
_VOLS = [100000 - i * 10 for i in range(40)]
_DATES = pd.bdate_range("2026-01-01", periods=40)


def _golden_df() -> pd.DataFrame:
    return pd.DataFrame({
        "date": _DATES, "open": _CLOSES,
        "high": [c + 5 for c in _CLOSES], "low": [c - 5 for c in _CLOSES],
        "close": _CLOSES, "volume": _VOLS,
    })


# === kelly_size: 現式 int(balance*alloc/price/100)*100 の golden ===

class TestKellySizeCharacterization:
    @pytest.mark.parametrize("consensus,balance,price,expected", [
        (3, 1_000_000, 1000.0, 100),    # 100k/1000/100=1
        (4, 1_000_000, 1000.0, 100),    # 150k/1000/100=1.5→1
        (5, 1_000_000, 1000.0, 200),    # 200k/1000/100=2
        (5, 1_000_000, 500.0, 400),     # 200k/500/100=4
        (3, 1_000_000, 3000.0, 0),      # 33.3→0 (F0真因のバグ挙動を golden 化)
        (3, 1_000_000, 50000.0, 0),
        (2, 1_000_000, 1000.0, 0),      # consensus<3
        (3, 0, 1000.0, 0),              # balance<=0
        (3, 1_000_000, 0.0, 0),         # price<=0
    ])
    def test_kelly_size_golden(self, consensus, balance, price, expected):
        assert kelly_size(consensus, balance, price) == expected


# === _compute_indicators: 決定論入力での末尾行 golden ===

class TestComputeIndicatorsCharacterization:
    def test_last_row_golden(self):
        ind = _compute_indicators(_golden_df()).iloc[-1]
        assert ind["ma25"] == pytest.approx(786.4, abs=1e-9)
        assert ind["deviation"] == pytest.approx(-0.09460834181078329, abs=1e-12)
        assert ind["rsi"] == pytest.approx(0.0, abs=1e-12)        # 単調下降→全loss
        assert ind["bb_mean"] == pytest.approx(786.4, abs=1e-9)
        assert ind["bb_std"] == pytest.approx(55.36846274429757, abs=1e-9)
        assert ind["bb_lower"] == pytest.approx(675.6630745114048, abs=1e-9)
        assert bool(ind["vol_decreasing"]) is True


# === F1: セクター閾値の死にコード現実を固定 ===

class TestSectorThresholdF1:
    def test_live_5digit_always_default(self):
        # live は 5桁 Code を渡す → 13銘柄map(4桁キー)に当たらず DEFAULT
        assert get_threshold("45190") == -0.07
        assert get_threshold("99999") == -0.07

    def test_4digit_map_is_unreachable_from_live(self):
        # 4桁なら map に当たる（が live は 5桁なので到達しない＝死にコード）
        assert get_threshold("4519") == -0.05


# === vote_all: 決定論入力での投票結果 golden ===

class TestVoteAllCharacterization:
    def _vote(self, ticker: str, nk_ma25: float) -> dict:
        mem = duckdb.connect(":memory:")
        _create_tables(mem)
        try:
            loop = asyncio.new_event_loop()
            try:
                return loop.run_until_complete(
                    vote_all(ticker, _golden_df(), 0.0, nikkei_ma25_dev=nk_ma25, con=mem)
                )
            finally:
                loop.close()
        finally:
            mem.close()

    def test_bearish_regime_golden(self):
        # nk_ma25<0 → p4=True。ticker=5桁→閾値-0.07、deviation-0.0946<=-0.07→p1
        # 2026-05-20: voting.py に votes["dev"] = ma25乖離率 を追加（kelly_node decision_shadow 用）
        result = self._vote("45190", -0.05)
        assert {k: v for k, v in result.items() if k != "dev"} == {
            "p1": True, "p2": True, "p3": False, "p4": True, "p5": True,
            "consensus": 4,
        }
        assert result["dev"] == pytest.approx(-0.09460834181078329, abs=1e-9)

    def test_bullish_regime_golden(self):
        # nk_ma25>=0 → p4=False。consensus が1減る
        result = self._vote("45190", 0.05)
        assert {k: v for k, v in result.items() if k != "dev"} == {
            "p1": True, "p2": True, "p3": False, "p4": False, "p5": True,
            "consensus": 3,
        }
        assert result["dev"] == pytest.approx(-0.09460834181078329, abs=1e-9)


# === S6 Lane B: p01 の mode 分岐（意図的挙動変更を固定）===
# plan受入4: S6は特性を意図的に更新。旧(static=ほぼ-0.07) と
# 新(ksigma=業種別) を両方テストで固定し「変えてよい変更」を明示。
# 実測差分(2026-05-16): live P1発火 52→126銘柄(+74、全て発火増方向)。

class TestLaneBS6:
    def _run_p01(self, df, ticker):
        import asyncio
        loop = asyncio.new_event_loop()
        try:
            return loop.run_until_complete(p01_mod.vote(df, ticker=ticker))
        finally:
            loop.close()

    def test_static_mode_is_rollback_path(self, monkeypatch):
        """mode='static' → 旧 config.sector_config.get_threshold（ロールバック）。"""
        monkeypatch.setattr(p01_mod, "DEFAULTS",
                            SimpleNamespace(sector_threshold_mode="static",
                                            default_sector_th=-0.07))
        # 4桁 '4519' は静的mapで -0.05。deviation -0.06 は -0.05 以下→発火
        df = _golden_df()
        # static で 4桁ticker は -0.05 閾値（get_threshold 経路の証明）
        called = {}
        monkeypatch.setattr(p01_mod, "get_threshold",
                            lambda t: (called.setdefault("t", t), -0.05)[1])
        self._run_p01(df, "4519")
        assert called["t"] == "4519"  # static は get_threshold を通る

    def test_ksigma_mode_uses_live_threshold(self, monkeypatch):
        """mode='ksigma'(既定) → data.sector_thresholds.live_threshold（業種別kσ）。"""
        monkeypatch.setattr(p01_mod, "DEFAULTS",
                            SimpleNamespace(sector_threshold_mode="ksigma",
                                            default_sector_th=-0.07))
        seen = {}
        monkeypatch.setattr(p01_mod, "live_threshold",
                            lambda t: (seen.setdefault("t", t), -0.03)[1])
        # deviation(_golden_df 末尾)= -0.0946 <= -0.03 → 発火
        assert self._run_p01(_golden_df(), "13320") is True
        assert seen["t"] == "13320"  # ksigma は live_threshold を通る（get_threshold不使用）


# === G3 凍結基準: 新universe(1,337) × コスト込み（③動的調整の唯一の物差し）===
# 旧S2 golden(494手選び・コスト無 484/477/60.0%/-11.6%)は S7 で**意図的に
# 無効化**(Grove承認・G3ゲート)。旧は二重に楽観だった: ①コスト無視(S7) ②
# 手選び494の生存バイアス(S7拡張で露見)。本テストが新frozen基準＝③が超える
# べき現実値。end_date を 2026-05-15 に固定し cache 増分でdriftしないよう再現性確保。
# 2026-05-17 実測（universe_snapshot 1,337・slippage10bps+立花手数料往復）。

@pytest.mark.integration
class TestBacktest3yBaseline:
    # 2026-05-20 更新: commit 8af3315 で entry 条件に p4_required overlay が追加され、
    # `p4_required=False` 経路の semantics が変わった（旧: 常に p4 必須 → 新: p4 を要求しない）。
    # 旧 baseline 555 trades は p4 必須時点の値で obsolete。新 baseline (901 trades) で pin し直す。
    # BASELINE_END 固定 + as_of_date 経由で sector_thresholds と Nikkei cache の drift から保護。
    BASELINE_END = "2026-05-15"

    def test_c3_baseline_frozen_g3(self):
        from src.backtest.runner import compute_sector_thresholds_from_cache
        from src.backtest.engine import run_backtest

        th = compute_sector_thresholds_from_cache(k=2.0, as_of_date=self.BASELINE_END)
        assert len(th) == 32  # 1,337universe の業種数
        res = run_backtest(sector_thresholds=th, max_concurrent_positions=7,
                           consensus_min=3, p4_required=False, p4_threshold=0.0,
                           end_date=self.BASELINE_END,
                           slippage_bps=10.0, apply_commission=True)
        m = res.metrics()
        # 2026-05-27: kabu commission ¥0 切替後の new baseline
        # (令和式 BNF era、calc_commission=0 で backtest pnl が gross 寄りに)
        assert m["total_trades"] == 901
        assert m["closed"] == 894
        assert m["wins"] == 485  # 旧482 → 485 (commission削減で勝利数増)
        assert m["losses"] == 409  # 旧412 → 409
        assert m["win_rate"] == pytest.approx(0.5425055928411633, abs=1e-9)
        assert m["avg_return"] == pytest.approx(0.004653781657914623, abs=1e-9)
        assert m["median_return"] == pytest.approx(0.01454384621052739, abs=1e-9)
        assert m["best"] == pytest.approx(0.46436585528473623, abs=1e-9)
        assert m["worst"] == pytest.approx(-0.21411562217333358, abs=1e-9)
        assert m["std"] == pytest.approx(0.08395881883433393, abs=1e-9)
        assert m["sharpe_per_trade"] == pytest.approx(0.05542933693597314, abs=1e-9)
        assert m["max_drawdown"] == pytest.approx(-0.24584118666325863, abs=1e-9)
        assert m["exit_reasons"] == {"take_profit": 412, "stop_loss": 267, "max_hold": 215}
