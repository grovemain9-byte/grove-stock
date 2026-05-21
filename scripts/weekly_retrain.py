"""Weekly retrain — PLT 再集計 + EVS 重み再最適化.

Phase 1 (Grove 2026-05-21):
火曜稼働の翌週金曜 (n+50 trades 想定) から動かす想定。
週次 cron で:
  1. bootstrap_plt.py 相当: positions(closed) + scans + decision_shadow.evs_components_json
     を JOIN し、PLT 全 cell を再集計
  2. optimizer.py: 既存 decision_shadow の cf_pnl_pct を入力に scipy で重み最適化
  3. 結果を data/evs_weights.json に書き戻し (evs.py が起動時に読む拡張は別途)

Idempotent: 何回実行しても同じデータで同じ結果。
Safe: optimizer が n<30 なら skip ログだけ吐く。

Usage:
    .venv/bin/python scripts/weekly_retrain.py [--db data/grove_stock.duckdb] [--weights data/evs_weights.json]
"""
from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("weekly_retrain")


def main() -> int:
    parser = argparse.ArgumentParser(description="Weekly PLT + weights retrain.")
    parser.add_argument("--db", default="data/grove_stock.duckdb")
    parser.add_argument("--weights", default="data/evs_weights.json")
    parser.add_argument("--skip-plt", action="store_true", help="Skip PLT re-aggregation")
    parser.add_argument("--skip-optimizer", action="store_true", help="Skip weight optimization")
    args = parser.parse_args()

    # Step 1: PLT re-aggregation (reuses bootstrap_plt logic)
    if not args.skip_plt:
        try:
            from scripts.bootstrap_plt import bootstrap
            summary = bootstrap(args.db, dry_run=False, verbose=True)
            logger.info("PLT retrain done: %s", summary)
        except Exception as e:
            logger.exception("PLT retrain FAILED: %s", e)
    else:
        logger.info("Skipping PLT retrain (--skip-plt)")

    # Step 2: EVS weight optimization
    if not args.skip_optimizer:
        try:
            from src.sizing.optimizer import optimize_weights, persist_weights
            result = optimize_weights(args.db, verbose=True)
            if result is None:
                logger.info("Optimizer skipped (insufficient samples).")
            else:
                persist_weights(result, args.weights)
                logger.info(
                    "Weights persisted to %s. Improvement: %+.4f Sharpe.",
                    args.weights, result.improvement,
                )
        except Exception as e:
            logger.exception("Optimizer FAILED: %s", e)
    else:
        logger.info("Skipping optimizer (--skip-optimizer)")

    return 0


if __name__ == "__main__":
    sys.exit(main())
