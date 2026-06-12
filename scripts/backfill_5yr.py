"""日足cache 3年→5年 backfill（a1決定・J-Quants Light=5年・本田EMA900検証の前提）。

重要（trace済・前提）: src/data/cache.py fetch_and_cache の incremental skip は
MAX(date)のみ見て**前方追記専用**。旧史を掘るには force=True 必須
（naiveな years=5 は fresh銘柄をskip＝silent no-op＝「494 stale」同型の罠）。
force=True → start=今-5年、INSERT OR IGNORE で旧2年分のみ冪等追加
（既存行は重複せず・前方の最新は既存cronが維持）。daily_update(cron)は不変
＝これは一回限りのbackfill、別関心事。
"""
from __future__ import annotations

import logging
import time

from src.data.cache import (
    load_universe, fetch_and_cache, fetch_and_cache_yfinance,
    cache_stats, _shared_client,
)

logger = logging.getLogger("backfill_5yr")


def main():
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    codes = list(load_universe()["Code"])
    logger.info("backfill 5yr start. cache before: %s", cache_stats())
    t0 = time.time()
    stats = {"ok_jq": 0, "ok_yf": 0, "err": 0}
    client = None
    try:
        client = _shared_client()
    except Exception as e:  # noqa: BLE001
        logger.warning("J-Quants client init失敗、yfinanceのみ: %s", e)

    for i, code in enumerate(codes):
        ok = False
        if client:
            try:
                fetch_and_cache(code, years=5, force=True, client=client)
                stats["ok_jq"] += 1
                ok = True
            except Exception as e:  # noqa: BLE001
                if "429" not in str(e):
                    logger.debug("jq fail %s: %s", code, str(e)[:60])
        if not ok:
            try:
                added = fetch_and_cache_yfinance(code, years=5)
                if added > 0:
                    stats["ok_yf"] += 1
                else:
                    stats["err"] += 1
            except Exception as e:  # noqa: BLE001
                stats["err"] += 1
                logger.warning("both failed %s: %s", code, str(e)[:60])
        if (i + 1) % 100 == 0:
            logger.info("progress %d/%d jq=%d yf=%d err=%d",
                        i + 1, len(codes), stats["ok_jq"],
                        stats["ok_yf"], stats["err"])

    logger.info("backfill done in %.0fs: %s", time.time() - t0, stats)
    logger.info("cache after: %s", cache_stats())
    logger.info("→ date_min が ~2021-05 まで遡れば EMA900(Y3)検証可。"
                "次: scripts.honda_strategy_wf 再実行で有効銘柄数を再確認。")


if __name__ == "__main__":
    main()
