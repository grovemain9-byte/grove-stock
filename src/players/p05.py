"""P5: 直近3日出来高が減少傾向（売り圧力の収束確認）。"""
from __future__ import annotations
import pandas as pd
from config.strategy_params import DEFAULTS


async def vote(df: pd.DataFrame, **kwargs) -> bool:
    """直近3日の出来高がvol[-3] > vol[-2] > vol[-1]ならTrue。S3: lookback単一ソース参照（現値3）。"""
    try:
        volume = df["volume"].astype(float)

        if len(volume) < DEFAULTS.p5_vol_lookback:
            return False

        v3, v2, v1 = volume.iloc[-3], volume.iloc[-2], volume.iloc[-1]
        return bool(v3 > v2 > v1)
    except Exception:
        return False
