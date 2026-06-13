"""S6 Lane B: live P1 のセクター閾値を業種別kσに統一。

背景（plan Lane B / 敵対F1）: backtest engine は既に runner.
compute_sector_thresholds_from_cache(k=2.0) の業種別kσを使用。一方 live p01 は
config.sector_config.get_threshold で、5桁Code が4桁mapに当たらず**全銘柄一律
-0.07**（13銘柄mapは死にコード）。本モジュールで live を backtest と同じ kσ に
統一する（B1決定）。これにより本日測定の3年基準(c=3)が live の正式基準になる。

DEFAULTS.sector_threshold_mode == "static" で旧経路に即ロールバック可。

F2教訓: 銘柄毎にDB/universeを引かない。Code→閾値マップをプロセス内で1回だけ
構築しメモ化（短命なscanプロセスで494銘柄を1ロードに）。
"""
from __future__ import annotations

import logging

import duckdb

from src.data.cache import CACHE_DB, load_universe
from config.strategy_params import DEFAULTS

logger = logging.getLogger("data.sector_thresholds")

_DDL = """
CREATE TABLE IF NOT EXISTS sector_thresholds (
    sector      VARCHAR PRIMARY KEY,
    threshold   DOUBLE NOT NULL,
    computed_at TIMESTAMP DEFAULT now()
)
"""

# プロセス内メモ（Code→threshold）。最初の参照で1回だけ構築。
_MAP_CACHE: dict[str, float] | None = None


def compute_and_store(k: float | None = None) -> dict[str, float]:
    """業種別kσ閾値を算出し CACHE_DB の sector_thresholds に保存（冪等: 全入替）。

    daily_update 後に呼ぶ想定。Returns: {S33Nm: threshold}。
    """
    from src.backtest.runner import compute_sector_thresholds_from_cache

    kk = DEFAULTS.sector_k if k is None else k
    th = compute_sector_thresholds_from_cache(k=kk)
    con = duckdb.connect(str(CACHE_DB))
    try:
        con.execute(_DDL)
        con.execute("DELETE FROM sector_thresholds")
        for sector, t in th.items():
            con.execute(
                "INSERT INTO sector_thresholds (sector, threshold) VALUES (?, ?)",
                [sector, float(t)],
            )
    finally:
        con.close()
    refresh()
    logger.info("sector_thresholds stored: %d sectors (k=%.2f)", len(th), kk)
    return th


def refresh() -> None:
    """メモを破棄（compute_and_store 後 / テストで強制再構築）。"""
    global _MAP_CACHE
    _MAP_CACHE = None


def _threshold_map() -> dict[str, float]:
    """Code→閾値 をプロセス内で1回だけ構築（F2: 銘柄毎ロード禁止）。

    universe(Code→S33Nm) と sector_thresholds(S33Nm→th) を結合。
    テーブル未生成/未登録業種は呼び出し側で default にフォールバック。
    """
    global _MAP_CACHE
    if _MAP_CACHE is not None:
        return _MAP_CACHE
    sector_th: dict[str, float] = {}
    try:
        con = duckdb.connect(str(CACHE_DB), read_only=True)
        try:
            rows = con.execute(
                "SELECT sector, threshold FROM sector_thresholds"
            ).fetchall()
            sector_th = {s: float(t) for s, t in rows}
        finally:
            con.close()
    except duckdb.Error:
        sector_th = {}  # テーブル未生成 → 空（全て default フォールバック）

    code_map: dict[str, float] = {}
    if sector_th:
        try:
            uni = load_universe()
            for _, r in uni.iterrows():
                t = sector_th.get(r["S33Nm"])
                if t is not None:
                    code_map[str(r["Code"])] = t
        except Exception:  # noqa: BLE001 — universe無し等はdefaultフォールバック
            code_map = {}
    _MAP_CACHE = code_map
    return code_map


def live_threshold(ticker: str) -> float:
    """live P1 用のセクター閾値。kσマップ→無ければ DEFAULTS.default_sector_th。"""
    return _threshold_map().get(ticker, DEFAULTS.default_sector_th)
