"""S7: Prime全銘柄 universe 再構築（二重取得排除・時差バッチ・再開可能）。

設計（Grove確定 2026-05-17）:
  1. fetch_universe で Prime全(~1,571)コード取得（get_list + 429 backoff）
  2. **bulk_fetch で3年barsを一度だけ取得**（時差バッチ＋cooldown）。
     fetch_and_cache は既cached freshをskip＝**再実行で続きから**。429 backoff内蔵
  3. ADV(20日)は**キャッシュ済 daily_quotes.value から算出**（API呼出ゼロ。
     旧版の「ADV用に別API + bulkで再取得」の二重取得を排除＝J-Quants負荷半減）
  4. ADV>=閾値でフィルタ → MIN_SANE安全網 → universe_snapshot 置換

不可逆は universe_snapshot 置換のみ（barは増分・既存破壊なし）。bars全取得
完了まで save しない＝494保護。backup: history_cache.duckdb.pre_s7_backup_*。
J-Quants Light レート制限は bulk_fetch の 429 backoff + バッチ間 cooldown +
`--max-batches N`（今Nバッチ→後で続き）で時差完走。
"""
from __future__ import annotations

import argparse
import logging
import time

from src.data.universe import (
    fetch_universe, prime_codes_cached, compute_adv_from_cache,
)
from src.data.cache import save_universe, bulk_fetch, cache_stats
from config.strategy_params import DEFAULTS

logger = logging.getLogger("build_universe")

MIN_SANE_UNIVERSE = 300  # これ未満は異常 → 494を上書きしない安全網


def build(adv_min_jpy: float | None = None, *,
          batch_size: int = 150, cooldown_sec: float = 30.0,
          max_batches: int = 0, lookback_days: int = 20) -> dict:
    adv = DEFAULTS.adv_min_jpy if adv_min_jpy is None else adv_min_jpy
    market = DEFAULTS.universe_market[0]  # ("Prime",)

    t0 = time.time()
    logger.info("fetch_universe: market=%s scale=ALL", market)
    prime = fetch_universe(scale_categories=(), market=market)
    all_codes = [str(c) for c in prime["Code"].tolist()]
    total = len(all_codes)

    cached = prime_codes_cached(all_codes)
    pending = [c for c in all_codes if c not in cached]
    logger.info("Prime全 %d銘柄。bars既取得 %d / 未取得 %d "
                "(batch=%d cooldown=%.0fs max_batches=%s)",
                total, len(cached), len(pending), batch_size,
                cooldown_sec, max_batches or "∞")

    batches = 0
    i = 0
    while i < len(pending):
        if max_batches and batches >= max_batches:
            break
        batch = pending[i:i + batch_size]
        # bulk_fetch: 内部で fetch_and_cache(既cached skip) + 429 exp backoff
        fs = bulk_fetch(batch, years=3)
        batches += 1
        i += batch_size
        now_cached = prime_codes_cached(all_codes)
        logger.info("barsバッチ%d: ok=%s 累計cached %d/%d (残%d)",
                    batches, fs.get("ok"), len(now_cached), total,
                    total - len(now_cached))
        more = i < len(pending) and not (max_batches and batches >= max_batches)
        if more and cooldown_sec > 0:
            logger.info("クールダウン %.0fs（429パンク防止）...", cooldown_sec)
            time.sleep(cooldown_sec)

    cached = prime_codes_cached(all_codes)
    # 完了判定 = max_batches で**打ち切られた**か否か（残pendingが残存）。
    # 「全コードが60本以上」を要求しない: 新規上場/データ僅少の数銘柄は
    # 60本に永遠に届かず build が永久未完になる。全pending取得**試行**済なら
    # 完了とし、データ過少コードは compute_adv_from_cache が自然除外する。
    truncated = bool(max_batches and batches >= max_batches and i < len(pending))
    if truncated:
        logger.info("bars未完了（max_batches打切）cached %d/%d。**再実行で続き"
                    "から**（save は全pending試行後・494無傷）。total %.0fs",
                    len(cached), total, time.time() - t0)
        return {"complete": False, "cached": len(cached), "total": total}

    # 全pending取得試行済 → ADVはキャッシュから算出（API呼出ゼロ）。
    # 60本未満の data-poor 銘柄は ADV 集計の HAVING/dropna で自然に除外。
    filtered = compute_adv_from_cache(prime, adv_min_jpy=adv,
                                      lookback_days=lookback_days)
    logger.info("bars全取得済 → ADVキャッシュ算出 → %d銘柄 (%.0fs)",
                len(filtered), time.time() - t0)
    if len(filtered) < MIN_SANE_UNIVERSE:
        raise RuntimeError(
            f"filtered {len(filtered)} < {MIN_SANE_UNIVERSE}（異常疑い）。"
            f"universe_snapshot を上書きせず中止。backup無傷。")

    save_universe(filtered)
    logger.info("universe_snapshot 置換: %d銘柄。cache: %s (total %.0fs)",
                len(filtered), cache_stats(), time.time() - t0)
    return {"complete": True, "universe": len(filtered),
            "cached": len(cached), "total": total}


def main():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    p = argparse.ArgumentParser(
        description="S7: Prime全 universe再構築（bars一括→ADVキャッシュ算出・時差バッチ）")
    p.add_argument("--adv", type=float, default=None, help="ADV下限(円)")
    p.add_argument("--batch-size", type=int, default=150,
                   help="barsバッチあたり銘柄数")
    p.add_argument("--cooldown", type=float, default=30.0,
                   help="バッチ間スリープ秒（429パンク防止）")
    p.add_argument("--max-batches", type=int, default=0,
                   help="このrunのバッチ数上限（0=全部・N=時差再開用）")
    args = p.parse_args()
    build(adv_min_jpy=args.adv, batch_size=args.batch_size,
          cooldown_sec=args.cooldown, max_batches=args.max_batches)


if __name__ == "__main__":
    main()
