"""S7: slippage+手数料込みの現実的バックテスト基準（engine.py 温存）。

敵対G3: 旧基準 `runner.py:57` は avg×n/3＝複利/サイズ/slippage/手数料なしの
脆い算術値。脆い基準を脆い調整が上回っても無意味。本モジュールは engine の
BacktestResult を**後処理**して取引毎に往復コストを差し引いた現実的指標を出す
（engine 本体は1行も変えない＝ground truth 温存・S0/S2 integration golden 維持）。

コスト前提（明示・assumption）:
- 手数料: 立花e支店 = calc_commission(片道, 最低¥110, 0.1%+税)。往復2回。
- slippage: 既定 10bps/片道（ADV流動性フィルタ後の終値スイング想定。
  保守側）。entry は不利に約定(×(1+s))、exit も不利(×(1-s))。
- position_size は engine 既定（¥100,000/建玉）。

新universe(S7)でこれを回した c=3 が ③ 動的調整の唯一の比較基準（凍結対象）。
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd

from src.data.db import calc_commission

DEFAULT_SLIPPAGE_BPS = 10.0  # 片道。assumption（保守）。変更時は build_diary に記録


@dataclass
class RealisticMetrics:
    """**per-trade** のコスト後指標のみ（ポートフォリオ指標は出さない）。

    注意: コスト後の*ポートフォリオ* maxDD/equity は engine の同時保有・
    mark-to-market ロジックを通さないと正しく出ない（単一notionalに全取引を
    累積する素朴計算は退化して無意味な大DDになる＝手法アーティファクト）。
    ポートフォリオ・コスト後基準は engine.run_backtest の opt-in コストで測る。
    本クラスは「1取引あたりコストがどれだけ削るか」の妥当な要約に限定。
    """
    closed: int
    wins: int
    losses: int
    win_rate: float
    avg_return: float          # コスト後 平均/取引
    median_return: float
    raw_avg_return: float      # 参考: コスト前（engine raw）
    cost_drag_per_trade: float # 平均で1取引あたり何%削れたか
    slippage_bps: float
    position_size: float


def _round_trip_cost_pct(entry_price: float, raw_pnl: float,
                         position_size: float, slip: float) -> tuple[float, float]:
    """(往復手数料%, slippage往復%) を position_size 比で返す。"""
    if entry_price <= 0:
        return 0.0, 0.0
    shares = position_size / entry_price
    exit_price = entry_price * (1.0 + raw_pnl)
    comm = calc_commission(entry_price, shares) + calc_commission(exit_price, shares)
    comm_pct = comm / position_size
    # entry ×(1+s) で買い、exit ×(1−s) で売る → リターン悪化分（乗法）
    slip_pct = (1.0 + raw_pnl) - (1.0 + raw_pnl) * (1.0 - slip) / (1.0 + slip)
    return comm_pct, slip_pct


def adjust(result, *, position_size: float = 100_000.0,
           slippage_bps: float = DEFAULT_SLIPPAGE_BPS) -> RealisticMetrics:
    """engine.BacktestResult → slippage+手数料控除後の現実的指標。"""
    closed = [t for t in result.trades if t.pnl_pct is not None]
    if not closed:
        return RealisticMetrics(0, 0, 0, 0.0, 0.0, 0.0, 0.0, 0.0,
                                slippage_bps, position_size)
    slip = slippage_bps / 10_000.0
    adj_pnls, raw_pnls = [], []
    for t in closed:
        raw = float(t.pnl_pct)
        comm_pct, slip_pct = _round_trip_cost_pct(
            float(t.entry_price), raw, position_size, slip)
        adj_pnls.append(raw - comm_pct - slip_pct)
        raw_pnls.append(raw)
    adj = np.array(adj_pnls)
    raw = np.array(raw_pnls)
    wins = adj > 0
    return RealisticMetrics(
        closed=len(closed),
        wins=int(wins.sum()),
        losses=int((~wins).sum()),
        win_rate=float(wins.mean()),
        avg_return=float(adj.mean()),
        median_return=float(np.median(adj)),
        raw_avg_return=float(raw.mean()),
        cost_drag_per_trade=float(raw.mean() - adj.mean()),
        slippage_bps=slippage_bps,
        position_size=position_size,
    )
