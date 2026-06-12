"""P2: RSI(14) < 35。"""
from __future__ import annotations
import pandas as pd
import ta.momentum
from config.strategy_params import DEFAULTS


async def vote(df: pd.DataFrame, **kwargs) -> bool:
    """RSI(14)が35未満ならTrue。S3: 単一ソース参照（現値14/35と同一）。"""
    try:
        close = df["close"].astype(float)
        rsi_indicator = ta.momentum.RSIIndicator(close, window=DEFAULTS.rsi_period)
        rsi = rsi_indicator.rsi().iloc[-1]

        if pd.isna(rsi):
            return False

        return bool(rsi < DEFAULTS.rsi_threshold)
    except Exception:
        return False
