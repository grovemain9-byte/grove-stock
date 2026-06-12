"""P3: close < ボリンジャーバンド下限(25, 2)。"""
from __future__ import annotations
import pandas as pd
import ta.volatility
from config.strategy_params import DEFAULTS


async def vote(df: pd.DataFrame, **kwargs) -> bool:
    """終値がBB下限を下回ればTrue。S3: 単一ソース参照（現値25/2と同一）。"""
    try:
        close = df["close"].astype(float)
        bb = ta.volatility.BollingerBands(
            close, window=DEFAULTS.bb_period, window_dev=DEFAULTS.bb_std)
        lower = bb.bollinger_lband().iloc[-1]

        if pd.isna(lower):
            return False

        return bool(close.iloc[-1] < lower)
    except Exception:
        return False
