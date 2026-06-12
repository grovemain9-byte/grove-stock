"""walk_forward (plan S5 §2.3): purged walk-forward CV + 指標レンズ。

目的: 「動的param調整」が「固定基準(S7後の新c=3)」を out-of-sample で
上回るかを過学習なく検証する土台。mlfinlab は重く要ライセンス → 自前軽量
（sklearn 依存も追加しない）。保有最大 max_hold_days のリーク窓を embargo で
purge する（train末尾と test先頭の間に max_hold 営業日の空白）。

engine.py は ground truth。empyrical は OOS equity に対する**追加レンズ**で
あり engine.metrics() を置換しない（plan 温存原則）。

注意（敵対的レビュー G3/G4）: 単一相場 walk-forward は過学習を検出しきれない。
本モジュールは「分割と指標」の正しい primitive のみ提供。採用判定
（複数レジーム fold + bootstrap CI + multiple-testing 補正）は本体 council 側。
"""
from __future__ import annotations

from typing import Iterator

import numpy as np
import pandas as pd
import empyrical

from config.strategy_params import DEFAULTS


def purged_splits(
    n: int, *, n_splits: int = 5, embargo: int | None = None,
) -> Iterator[tuple[np.ndarray, np.ndarray]]:
    """時系列 expanding-window 分割。train末尾と test先頭に embargo の空白。

    embargo 既定 = DEFAULTS.max_hold_days（保有が跨ぐリーク窓）。
    各 fold: train=[0, cut-embargo), test=[cut, next_cut)。空 fold は yield しない。
    """
    if embargo is None:
        embargo = DEFAULTS.max_hold_days
    if n_splits < 1 or n < (n_splits + 1):
        return
    bounds = np.linspace(0, n, n_splits + 1, dtype=int)
    for k in range(1, n_splits + 1):
        cut = bounds[k]
        nxt = bounds[k + 1] if k < n_splits else n
        train_end = cut - embargo
        if train_end <= 0 or nxt <= cut:
            continue
        train = np.arange(0, train_end)
        test = np.arange(cut, nxt)
        if len(train) == 0 or len(test) == 0:
            continue
        yield train, test


def metrics_from_equity(equity: pd.Series) -> dict:
    """OOS equity から指標を算出（empyrical 追加レンズ + engine式 maxDD 併記）。"""
    rets = equity.pct_change().dropna()
    if len(rets) < 2:
        return {"n": len(rets)}
    peak = equity.cummax()
    engine_mdd = float(((equity - peak) / peak).min())  # engine.metrics() と同式
    return {
        "n": int(len(rets)),
        "annual_return": float(empyrical.annual_return(rets)),
        "sharpe": float(empyrical.sharpe_ratio(rets)),
        "sortino": float(empyrical.sortino_ratio(rets)),
        "calmar": float(empyrical.calmar_ratio(rets)),
        "max_drawdown_empyrical": float(empyrical.max_drawdown(rets)),
        "max_drawdown_engine": engine_mdd,  # ground truth 併記（一致するはず）
    }


def compare(dynamic_equity: pd.Series, baseline_equity: pd.Series) -> dict:
    """動的調整 vs 固定基準の OOS 指標差分（primitive）。

    採用可否はここで決めない（council が複数レジーム/bootstrap/補正で判断）。
    返すのは per-metric の (dynamic, baseline, delta) のみ。
    """
    d = metrics_from_equity(dynamic_equity)
    b = metrics_from_equity(baseline_equity)
    keys = ("annual_return", "sharpe", "sortino", "calmar", "max_drawdown_engine")
    out: dict[str, dict] = {}
    for kk in keys:
        if kk in d and kk in b:
            out[kk] = {"dynamic": d[kk], "baseline": b[kk],
                       "delta": d[kk] - b[kk]}
    out["_meta"] = {"dynamic_n": d.get("n", 0), "baseline_n": b.get("n", 0)}
    return out
