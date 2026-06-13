"""HB（heartbeat）自律学習ランナー — "自律的に育つ会社"の脈。

cron から人手ゼロで起動し、組織の成長メトリクスを自動蓄積する:
  --daily : capital_tracker.snapshot（資金回転把握）+ decision_shadow
            backfill（go/pass の事後反実仮想を出口日基準で確定）
  --weekly: hypothesis_loop（Shadow=記録のみ・適用ゼロ）+ A/B集計
            （regime_filter treatment{p5m} vs control{p2m,p10m}）

全て shadow/記録/read-only。strategy_params も実資金も**一切変更しない**
（昇格/適用は Grove ゲート）。KAIROS: 異常は記録して継続、危険操作なし。
"""
from __future__ import annotations

import argparse
import logging
from datetime import date

from src.data.db import get_connection
from src.measurement import capital_tracker, decision_shadow, hypothesis_loop

logger = logging.getLogger("hb_learning")


def _ab_summary(db_path: str | None = None) -> list[tuple]:
    """positions を book 別に集計し regime_filter A/B を比較（決定論・読取のみ）。"""
    con = get_connection(db_path)
    try:
        rows = con.execute(
            "SELECT book, "
            "       count(*) FILTER (WHERE status='closed') closed, "
            "       count(*) FILTER (WHERE status='closed' AND pnl>0) wins, "
            "       COALESCE(SUM(pnl) FILTER (WHERE status='closed'),0) pnl "
            "FROM positions WHERE book NOT IN ('legacy') GROUP BY book ORDER BY book"
        ).fetchall()
    finally:
        con.close()
    return rows


def run_daily(db_path: str | None = None) -> dict:
    cap = capital_tracker.snapshot(db_path)
    filled = decision_shadow.backfill_counterfactuals(db_path=db_path)
    logger.info("[HB daily] capital snapshot %d books / decision_shadow "
                "cf確定 %d件", len(cap), filled)
    return {"capital_books": len(cap), "cf_filled": filled}


def run_weekly(db_path: str | None = None) -> dict:
    res = hypothesis_loop.run_loop(db_path=db_path)
    go = [r["name"] for r in res if r["verdict"] == "GO"]
    ab = _ab_summary(db_path)
    logger.info("[HB weekly] hypotheses評価 %d件 GO=%s（Shadow・未適用）",
                len(res), go or "なし")
    for book, closed, wins, pnl in ab:
        wr = (wins / closed * 100) if closed else 0.0
        logger.info("[HB weekly] A/B book=%s closed=%d 勝率=%.1f%% pnl=%.0f",
                    book, closed, wr, pnl)
    return {"hypotheses": len(res), "go": go, "ab": ab}


def main():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    p = argparse.ArgumentParser(description="HB自律学習ランナー（shadow・記録のみ）")
    g = p.add_mutually_exclusive_group(required=True)
    g.add_argument("--daily", action="store_true")
    g.add_argument("--weekly", action="store_true")
    args = p.parse_args()
    logger.info("[HB起動] %s (%s) — shadow/記録のみ・適用ゼロ",
                "daily" if args.daily else "weekly", date.today())
    if args.daily:
        r = run_daily()
    else:
        r = run_weekly()
    logger.info("[HB報告] %s", r)


if __name__ == "__main__":
    main()
