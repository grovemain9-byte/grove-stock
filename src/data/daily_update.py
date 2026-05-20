"""daily_update: 当日までの日足をcache (history_cache.duckdb) にincremental更新。
cronで大引け後 (15:10) に実行。
"""
from __future__ import annotations

import argparse
import logging
import time
from datetime import date

from src.data.cache import load_universe, fetch_and_cache, fetch_and_cache_yfinance, cache_stats, _shared_client, fetch_and_cache_nikkei

logger = logging.getLogger("data.daily_update")


def update_all(throttle_sec: float = 0.5, fallback_yfinance: bool = True) -> dict:
    """universe全銘柄についてキャッシュを今日まで更新。"""
    universe = load_universe()
    codes = list(universe["Code"])
    stats = {"ok_jquants": 0, "ok_yfinance": 0, "err": 0, "skip": 0}
    client = None
    try:
        client = _shared_client()
    except Exception as e:
        logger.warning("client init failed, skip J-Quants: %s", e)

    for i, code in enumerate(codes):
        added = 0
        # 1st try: J-Quants incremental (ingestion差分)
        if client:
            try:
                added = fetch_and_cache(code, years=3, client=client)
                stats["ok_jquants"] += 1
                time.sleep(throttle_sec)
                continue
            except Exception as e:
                if "429" not in str(e):
                    logger.debug("jquants fail %s: %s", code, str(e)[:60])
        # 2nd try: yfinance
        if fallback_yfinance:
            try:
                added = fetch_and_cache_yfinance(code, years=3)
                if added > 0:
                    stats["ok_yfinance"] += 1
                else:
                    stats["skip"] += 1
            except Exception as e:
                stats["err"] += 1
                logger.warning("both failed %s: %s", code, str(e)[:60])
        else:
            stats["err"] += 1
        if (i+1) % 50 == 0:
            logger.info("daily_update progress: %d/%d jq=%d yf=%d err=%d",
                        i+1, len(codes), stats["ok_jquants"], stats["ok_yfinance"], stats["err"])
    return stats


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    p = argparse.ArgumentParser()
    p.add_argument("--throttle", type=float, default=0.5)
    p.add_argument("--no-yfinance", action="store_true")
    args = p.parse_args()

    logger.info("daily_update start. cache before: %s", cache_stats())
    t0 = time.time()
    stats = update_all(throttle_sec=args.throttle, fallback_yfinance=not args.no_yfinance)
    logger.info("daily_update done in %.0fs: %s", time.time()-t0, stats)
    logger.info("cache after: %s", cache_stats())

    # S6 Lane B: 最新キャッシュで業種別kσ閾値を再計算し保存（翌寄りのscanが読む）
    try:
        from src.data.sector_thresholds import compute_and_store
        th = compute_and_store()
        logger.info("sector kσ thresholds recomputed: %d sectors", len(th))
    except Exception as e:  # noqa: BLE001 — 閾値再計算失敗でcache更新は無効化しない
        logger.error("sector_thresholds compute failed (live keeps prev/default): %s", e)

    # 2026-05-20: Nikkei 225 cache を upsert (backtest engine が cache 優先で読む)。
    # 失敗しても scan/cache update は無効化しない（fallback で yfinance moving window が走る）
    try:
        n_nikkei = fetch_and_cache_nikkei(years=5)
        logger.info("nikkei_bars upserted: %d rows", n_nikkei)
    except Exception as e:  # noqa: BLE001
        logger.error("nikkei_bars upsert failed (engine will fallback to moving window): %s", e)


if __name__ == "__main__":
    main()
