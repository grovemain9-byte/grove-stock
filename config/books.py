"""ペーパー資金ブック定義（3資金規模 ¥2M/¥5M/¥10M を並列稼働 + 令和式 per-book playbook）。

Grove方針 (2026-05-16):
- 100株単位は維持（単元未満株にしない）
- 当初 ¥1M/¥5M/¥10M/¥30M/¥50M を並列稼働。2026-06-14 Grove: 最低単元¥2M化に伴い
  ¥2M/¥5M/¥10M の3 book に整理（p1m/p30m/p50m 削除＝運用簡素化）。各ブック複利。
- 少額ブック(¥1M/¥5M)は固定10/15/20%規律だと1単元すら買えずエントリー不能
  → flex=True: 最低1単元保証（上限なし=cashが許す限り）。集中度/回収判断は
    将来 council(本体) が strategy_params 経由で動的に決める前提のレバー。
- 大型ブック(¥10M+)は固定%規律を維持（trading-rules Tiered Kelly準拠）。

2026-05-25 令和式 per-book playbook (Layer 7-10):
- Grove vision「どんな資金からでも universal に勝てる」を実現するため、
  各 book に固有の playbook (universe filter + 戦略パラメータ) を持たせる。
- 5/12-5/25 forward paper retrospective n=198 で判明した事実に基づく:
  * consensus=3 全 book で loss-generator (avg -0.12% kabu)
  * consensus=4 で win 55-69%, avg +2.5-2.9% gross (decisive winner)
  * price band 3000-5000 が disaster (win 10.6%, -¥1.6M kabu)
  * price band 2000-3000 が prime hunting (win 52.5%, +¥313K kabu)
  * 5000+ は信頼できる勝者 (n小だが win 75-100%)
"""
from __future__ import annotations

from typing import NamedTuple


class Book(NamedTuple):
    book_id: str          # positions.book に記録するキー
    initial_capital: float
    flex: bool            # True=少額柔軟サイジング（最低1単元保証・上限なし）
    regime_filter: bool = False  # True=弱気時(p4)のみ建玉。A/B用 per-book overlay
    max_concurrent: int = 7  # 同時保有上限。資金規模に応じて分散度を変える

    # 2026-05-25 令和式 per-book playbook ---
    # Layer 7: Universe filter per book
    price_min: float = 500.0       # 銘柄最低価格 (これ未満skip)
    price_max: float = 50_000.0    # 銘柄最高価格 (これ超過skip。¥500K cap考慮)
    # Layer 8: Strategy params per book
    consensus_min_override: int = 4  # 令和式: consensus<4 は skip (旧 CLAUDE.md core =3、override で強化)

    # routing_priority は 2026-05-27 削除 (Layer 9 cross-book dedup削除に伴い)
    # 5 book は完全独立 A/B paper、優先順位の概念なし


# === 令和式 universal rules (全 book 共通) ===
# Layer 7: 全 book で skip する価格帯 (forward paper retrospective でlossゾーン)
SKIP_PRICE_RANGES: tuple[tuple[float, float], ...] = (
    (3000.0, 5000.0),  # win 10.6%, kabu PnL -¥1.6M (disaster zone)
)


# 2026-06-14 Grove: 最低単元¥2M化に伴い 3 book に整理（p1m/p30m/p50m 削除）。
# 「200万/500万/1000万の3つだけでいい、出ないと大変でしょ」=運用簡素化（edge最適化でなく整理）。
# 既存 p30m/p50m の open建玉は monitor（book非依存 WHERE status='open'）が正常決済 → orphan化しない。
# 昇順 (p2m,p5m,p10m)。auto_select_book は index でなく book_id 想定の 3-tier に書換済。
BOOKS: tuple[Book, ...] = (
    # p2m (¥2M, entry book / 最小単元): price_min=1000 で p1m敗因（flex最小単元×¥500未満
    # ゴミ帯=損失97.9%）を回避。cons4=勝book標準。max3=¥2M相応。
    Book(
        "p2m", 2_000_000.0, flex=True, regime_filter=False, max_concurrent=3,
        price_min=1_000.0, price_max=10_000.0,
        consensus_min_override=4,
    ),
    Book(
        "p5m", 5_000_000.0, flex=True, regime_filter=True, max_concurrent=10,
        price_min=500.0, price_max=5_000.0,    # +¥3-5K帯はSKIP_PRICE_RANGESで弾く
        consensus_min_override=4,
    ),
    Book(
        "p10m", 10_000_000.0, flex=False, regime_filter=False, max_concurrent=15,
        price_min=1_000.0, price_max=10_000.0,
        consensus_min_override=4,
    ),
)

LEGACY_BOOK = "legacy"  # マルチブック化以前の既存3ポジションのタグ


# === Layer 10: 動的 capital tier 検出 ===
# 任意の equity から最適 book playbook を auto-select
# (新規ユーザーが任意資金で起動 → 自動的に最適 playbook 適用)
def auto_select_book(equity: float) -> Book:
    """Equity (¥) から最適 book playbook を選択 (Grove vision: universal scalable)。

    Args:
        equity: 起動時 capital

    Returns:
        Book NamedTuple — equity に最も適した playbook

    例:
        auto_select_book(2_500_000)  → p2m  (< ¥3.5M)
        auto_select_book(5_000_000)  → p5m  (¥3.5M-7.5M)
        auto_select_book(15_000_000) → p10m (¥7.5M+)

    注: 本番呼出なし（2026-06-14時点、テストのみ）。tier境界は ¥2M/¥5M/¥10M の
    幾何中点（≈3.16M/7.07M）を丸めた判断値で live検証はされていない。
    """
    if equity < 3_500_000:
        return BOOKS[0]  # p2m: ~¥2M-3.5M
    elif equity < 7_500_000:
        return BOOKS[1]  # p5m: ¥3.5M-7.5M
    else:
        return BOOKS[2]  # p10m: ¥7.5M+


def is_price_in_skip_range(price: float) -> bool:
    """SKIP_PRICE_RANGES に該当するか判定 (Layer 7 universal)。"""
    return any(lo <= price < hi for lo, hi in SKIP_PRICE_RANGES)


def is_price_book_acceptable(price: float, book: Book) -> bool:
    """価格が book の price_min/price_max + 令和式 SKIP_PRICE_RANGES を満たすか。

    True: book で entry 可能 / False: skip
    """
    if price < book.price_min or price > book.price_max:
        return False
    if is_price_in_skip_range(price):
        return False
    return True
