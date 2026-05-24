"""ペーパー資金ブック定義（5資金規模を並列稼働）。

Grove方針 (2026-05-16):
- 100株単位は維持（単元未満株にしない）
- ¥1M/¥5M/¥10M/¥30M/¥50M を別ブックとして並列稼働。各ブック複利。
- 少額ブック(¥1M/¥5M)は固定10/15/20%規律だと1単元すら買えずエントリー不能
  → flex=True: 最低1単元保証（上限なし=cashが許す限り）。集中度/回収判断は
    将来 council(本体) が strategy_params 経由で動的に決める前提のレバー。
- 大型ブック(¥10M+)は固定%規律を維持（trading-rules Tiered Kelly準拠）。

注意: 本ファイルは Phase 0 の暫定単一ソース。S1 で strategy_params.py へ統合し
council が触れるフィールドにする（plan: Judgeのみ書込）。
"""
from __future__ import annotations

from typing import NamedTuple


class Book(NamedTuple):
    book_id: str          # positions.book に記録するキー
    initial_capital: float
    flex: bool            # True=少額柔軟サイジング（最低1単元保証・上限なし）
    regime_filter: bool = False  # True=弱気時(p4)のみ建玉。A/B用 per-book overlay
    max_concurrent: int = 7  # 同時保有上限。資金規模に応じて分散度を変える


# 2026-05-24: max_concurrent を book別に動的化（旧: 全book 7固定）
# 5/17 ユニバース拡大(494→1337銘柄)後、consensus=5 の最強シグナルが全book
# max_concurrent_full で全件pass される機能不全を発見。資金規模に比例した
# 分散上限に変更し、強シグナルが必ず席を取れるよう main.py で consensus
# DESC ソートも同時導入。
#
# regime_filter A/B（2026-05-17 Grove承認）: 継続ループが regime_filter を
# 3手法再現・3/3 walk-forward・損失年黒字化で GO 判定。全面適用でなく一部
# ブックに適用して実 paper で対照比較（残りは control＝データ生成も継続）。
# treatment = {p5m, p30m}（資金規模を散らす）/ control = {p1m, p10m, p50m}。
BOOKS: tuple[Book, ...] = (
    Book("p1m", 1_000_000.0, True, regime_filter=False, max_concurrent=2),   # control / ¥500K per slot
    Book("p5m", 5_000_000.0, True, regime_filter=True, max_concurrent=5),    # treatment / ¥1M per slot
    Book("p10m", 10_000_000.0, False, regime_filter=False, max_concurrent=7),  # control / ¥1.4M per slot (現状維持)
    Book("p30m", 30_000_000.0, False, regime_filter=True, max_concurrent=12),  # treatment / ¥2.5M per slot
    Book("p50m", 50_000_000.0, False, regime_filter=False, max_concurrent=20), # control / ¥2.5M per slot
)

LEGACY_BOOK = "legacy"  # マルチブック化以前の既存3ポジションのタグ
