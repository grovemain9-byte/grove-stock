"""Kelly サイジング。

Issue #6: consensus → ポジションサイズ（株数）。
CLAUDE.md: 損切り-5% / 1銘柄最大リスク2%。
"""
from __future__ import annotations

from config.strategy_params import DEFAULTS

# consensus別のポジション割合（対残高）。S3: 単一ソース参照（現値と同一）
CONSENSUS_ALLOCATION = DEFAULTS.kelly_alloc_map()


def kelly_size(
    consensus: int, balance: float, price: float, *, flex: bool = False
) -> int:
    """Kellyベースのポジションサイズ計算。

    Args:
        consensus: コンセンサス票数（0〜5）
        balance: サイジング基準額（equity。大型ブックは固定%の母数）
        price: 現在の株価
        flex: 少額ブック柔軟モード。固定%配分だと1単元(100株)すら買えない時、
              cashが許す限り最低1単元を保証する（上限なし）。集中度/回収の
              判断は将来 council が strategy_params 経由で動的決定する前提。
              既定False＝従来挙動完全維持（大型ブック・dry-run・live・既存テスト）。

    Returns:
        買い株数（0なら見送り）。100株単位に丸める。
        ※ cash上限ガードは呼び出し側(kelly_node)が累積で行う。
    """
    if consensus < 3 or balance <= 0 or price <= 0:
        return 0

    allocation = CONSENSUS_ALLOCATION.get(consensus, 0.20)
    position_value = balance * allocation

    # 100株単位に丸め
    shares = int(position_value / price / 100) * 100

    # 少額ブック: 固定%だと0株 → 最低1単元（cashが100株分あるなら）。
    # 上限は設けない（Grove方針: 集中度はエージェントが判断）。
    if flex and shares == 0 and balance >= price * 100:
        shares = 100

    return max(shares, 0)
