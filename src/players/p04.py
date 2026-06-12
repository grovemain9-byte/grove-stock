"""P4: 日経225が弱気レジーム（close < MA25）。
変更 2026-04-21: 旧「日経>-2%（ほぼ常時true）」→「日経MA25乖離率 < 0」
理由: 市場全体が下げトレンドの日のみ逆張りを発火させる。
"""
from __future__ import annotations
import pandas as pd
from config.strategy_params import DEFAULTS


async def vote(df: pd.DataFrame, **kwargs) -> bool:
    """日経225がMA25より下ならTrue（市場弱気レジーム）。S3: 閾値を単一ソース参照（現値0.0と同一）。"""
    nikkei_ma25_dev = kwargs.get("nikkei_ma25_dev")
    if nikkei_ma25_dev is None:
        return False
    try:
        return float(nikkei_ma25_dev) < DEFAULTS.p4_threshold
    except Exception:
        return False
