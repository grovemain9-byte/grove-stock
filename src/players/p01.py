"""P1: MA25乖離率 ≤ セクター閾値。

S6 Lane B: DEFAULTS.sector_threshold_mode で閾値ソースを切替。
  "ksigma"(既定) → 業種別kσ（backtestと統一。data.sector_thresholds）
  "static"       → 旧 config.sector_config（=ほぼ全銘柄-0.07。即ロールバック用）
"""
from __future__ import annotations
import pandas as pd
from config.sector_config import get_threshold
from config.strategy_params import DEFAULTS
from src.data.sector_thresholds import live_threshold


async def vote(df: pd.DataFrame, **kwargs) -> bool:
    """MA25乖離率がセクター閾値以下ならTrue。"""
    try:
        ticker: str = kwargs.get("ticker", "")
        if DEFAULTS.sector_threshold_mode == "static":
            threshold = get_threshold(ticker)        # 旧経路（ロールバック）
        else:
            threshold = live_threshold(ticker)       # S6: 業種別kσ

        close = df["close"].astype(float)
        ma25 = close.rolling(25).mean().iloc[-1]

        if pd.isna(ma25) or ma25 == 0:
            return False

        deviation = (close.iloc[-1] - ma25) / ma25
        return bool(deviation <= threshold)
    except Exception:
        return False
