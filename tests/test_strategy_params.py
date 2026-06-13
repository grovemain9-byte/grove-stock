"""S1: StrategyParams.DEFAULTS が現 LIVE ハードコード値の 1:1 写しであること。

これが緑 = DEFAULTS は現挙動を変えない「現値の写し」。S2/S3 で各モジュールを
DEFAULTS 参照に配線後も test_characterization が緑なら「集約は挙動保存」。
S1 時点では未配線（DEFAULTS を誰も使っていない）であることも確認する。
"""
from __future__ import annotations

import inspect

from config.strategy_params import DEFAULTS, StrategyParams


def test_defaults_is_frozen_singleton():
    import dataclasses
    assert dataclasses.is_dataclass(DEFAULTS)
    params = {f.name for f in dataclasses.fields(DEFAULTS)}
    # frozen: 代入不可
    import pytest
    with pytest.raises(dataclasses.FrozenInstanceError):
        DEFAULTS.stop_loss = -0.99  # type: ignore[misc]
    assert "kelly_alloc" in params


def test_matches_engine_constants():
    from src.backtest import engine as e
    assert DEFAULTS.ma_period == e.MA_PERIOD == 25
    assert DEFAULTS.rsi_period == e.RSI_PERIOD == 14
    assert DEFAULTS.bb_period == e.BB_PERIOD == 25
    assert DEFAULTS.bb_std == e.BB_STD == 2.0
    assert DEFAULTS.rsi_threshold == e.RSI_THRESHOLD == 35
    assert DEFAULTS.consensus_min == e.CONSENSUS_MIN == 3
    assert DEFAULTS.stop_loss == e.STOP_LOSS == -0.07
    assert DEFAULTS.max_hold_days == e.MAX_HOLD_DAYS == 15
    assert DEFAULTS.default_sector_th == e.DEFAULT_SECTOR_TH == -0.07


def test_matches_live_constants():
    from src.voting import CONSENSUS_THRESHOLD
    from src.monitor import STOP_LOSS_PCT, MAX_HOLD_DAYS
    from src.main import MAX_CONCURRENT_POSITIONS as MAIN_MAXC
    from src.kelly import CONSENSUS_ALLOCATION

    assert DEFAULTS.consensus_min == CONSENSUS_THRESHOLD == 3
    assert DEFAULTS.stop_loss == STOP_LOSS_PCT == -0.07
    assert DEFAULTS.max_hold_days == MAX_HOLD_DAYS == 15
    # max_concurrent は kelly_node 内で book別管理 (commit c9254b4, 2026-05-24)
    # main.py の MAX_CONCURRENT_POSITIONS は legacy fallback として保持 (=DEFAULTS.max_concurrent=7)
    assert DEFAULTS.max_concurrent == MAIN_MAXC == 7
    assert DEFAULTS.kelly_alloc_map() == CONSENSUS_ALLOCATION == {3: 0.10, 4: 0.15, 5: 0.20}


def test_matches_universe_and_sector_defaults():
    from src.data.universe import ADV_MIN_JPY
    from src.backtest.runner import compute_sector_thresholds_from_cache

    assert DEFAULTS.adv_min_jpy == ADV_MIN_JPY == 1e8
    assert DEFAULTS.universe_market == ("Prime",)
    # runner の kσ 既定 k と floor/ceiling リテラル（コード内 max(-0.20,min(-0.03,...))）
    sig = inspect.signature(compute_sector_thresholds_from_cache)
    assert sig.parameters["k"].default == DEFAULTS.sector_k == 2.0
    src = inspect.getsource(compute_sector_thresholds_from_cache)
    assert "max(-0.20, min(-0.03," in src  # DEFAULTS.th_floor/th_ceiling と一致
    assert DEFAULTS.th_floor == -0.20 and DEFAULTS.th_ceiling == -0.03
    assert DEFAULTS.sector_threshold_mode == "ksigma"


def test_books_single_source():
    from config.books import BOOKS
    assert DEFAULTS.books == BOOKS
    assert [b.book_id for b in DEFAULTS.books] == ["p1m", "p5m", "p10m", "p30m", "p50m", "p2m"]


def _wired_modules() -> set[str]:
    """src/ で strategy_params を実 import しているモジュール集合（コメント除外）。"""
    import subprocess
    repo = inspect.getfile(StrategyParams).rsplit("/config/", 1)[0]
    out = subprocess.run(
        ["grep", "-rlE", r"(from\s+config\.strategy_params|import\s+strategy_params)",
         "src/", "--include=*.py"],
        capture_output=True, text=True, cwd=repo,
    )
    return {p.strip() for p in out.stdout.splitlines() if p.strip()}


def test_wiring_stage_s6_complete():
    """配線段階トラッキング: S2+S3+S6 後の最終到達状態。

    S6 で p01 も DEFAULTS 参照（mode で kσ/static 切替）。これで config 集約の
    全モジュール配線が完了。挙動: S2/S3 は保存（golden緑）、S6 は p01 のみ
    意図的変更（test_characterization の Lane B テストが新挙動を固定）。
    """
    wired = _wired_modules()
    expected = {
        "src/backtest/engine.py", "src/voting.py", "src/monitor.py",
        "src/main.py", "src/kelly.py",
        "src/players/p01.py", "src/players/p02.py", "src/players/p03.py",
        "src/players/p04.py", "src/players/p05.py",
    }
    missing = expected - wired
    assert not missing, f"S6で配線完了しているべき未配線: {missing}"
