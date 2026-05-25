"""ペーパー資金ブック定義（5資金規模を並列稼働 + 令和式 per-book playbook）。

Grove方針 (2026-05-16):
- 100株単位は維持（単元未満株にしない）
- ¥1M/¥5M/¥10M/¥30M/¥50M を別ブックとして並列稼働。各ブック複利。
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

    # routing priority: dedup時の優先順位 (高いほど先取り、cross-book dedup用)
    routing_priority: int = 0  # 大資金=高、小資金=低


# === 令和式 universal rules (全 book 共通) ===
# Layer 7: 全 book で skip する価格帯 (forward paper retrospective でlossゾーン)
SKIP_PRICE_RANGES: tuple[tuple[float, float], ...] = (
    (3000.0, 5000.0),  # win 10.6%, kabu PnL -¥1.6M (disaster zone)
)


# 2026-05-25 令和式 per-book playbook 完全版
BOOKS: tuple[Book, ...] = (
    Book(
        "p1m", 1_000_000.0, flex=True, regime_filter=False, max_concurrent=4,
        price_min=500.0, price_max=3_000.0,   # 1単元 fits ¥1M資金 (¥3000×100=¥300K=30%cap)
        consensus_min_override=4,              # 令和式: strong signalのみ
        routing_priority=1,                    # 最後割当 (大資金優先)
    ),
    Book(
        "p5m", 5_000_000.0, flex=True, regime_filter=True, max_concurrent=10,
        price_min=500.0, price_max=5_000.0,    # +¥3-5K帯はSKIP_PRICE_RANGESで弾く
        consensus_min_override=4,
        routing_priority=2,
    ),
    Book(
        "p10m", 10_000_000.0, flex=False, regime_filter=False, max_concurrent=15,
        price_min=1_000.0, price_max=10_000.0,
        consensus_min_override=4,
        routing_priority=3,
    ),
    Book(
        "p30m", 30_000_000.0, flex=False, regime_filter=True, max_concurrent=20,
        price_min=1_000.0, price_max=20_000.0,
        consensus_min_override=4,
        routing_priority=4,
    ),
    Book(
        "p50m", 50_000_000.0, flex=False, regime_filter=False, max_concurrent=30,
        price_min=500.0, price_max=50_000.0,   # 制限最小
        consensus_min_override=4,
        routing_priority=5,                    # 最優先
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
        auto_select_book(2_500_000)  → p1m (1M-3M range)
        auto_select_book(15_000_000) → p10m (8M-20M range)
        auto_select_book(100_000_000) → p50m (¥40M+ range)
    """
    if equity < 3_000_000:
        return BOOKS[0]  # p1m: ¥1M-3M
    elif equity < 8_000_000:
        return BOOKS[1]  # p5m: ¥3M-8M
    elif equity < 20_000_000:
        return BOOKS[2]  # p10m: ¥8M-20M
    elif equity < 40_000_000:
        return BOOKS[3]  # p30m: ¥20M-40M
    else:
        return BOOKS[4]  # p50m: ¥40M+


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
