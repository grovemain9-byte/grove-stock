"""Sizing Router — PLT lookup + EVS fallback + ε-greedy exploration.

Phase 0+ (Grove 2026-05-21):
"成績表で動的に動く" の dispatcher。各 entry candidate に対して:
  1. features → CellKey
  2. PLT lookup (DB)
  3. cell.confidence == "warm" or "hot" → 90% cell cap使用, 10% explore via EVS
  4. cell.confidence == "cold" → 80% EVS cap, 20% explore (random uniform cap)
  5. cell が存在しない（未到達セル）→ EVS cap 採用、新セルとして cold 扱い

ε-greedy schedule (Grove 2026-05-21):
  global n_samples < 50 → ε = 20% (explore promotion)
  global n_samples ≥ 50 → ε = 10%

Output: SizingDecision dataclass with:
  - cap_pct: final 0-1 value to apply
  - source: "plt_warm" | "plt_hot" | "evs_fallback" | "exploration"
  - cell_id: for downstream logging into decision_shadow

ε-greedy notes:
- Pseudo-random決定はticker+date hashで再現可能 (seed控除可能、production はsystem RNG)
- exploration_flag は decision_shadow に記録、後で「exploration trade群のEV」と
  「exploitation trade群のEV」を比較可能 (validity check)
"""
from __future__ import annotations

import hashlib
import logging
import random
from dataclasses import dataclass
from datetime import date
from typing import Optional

import duckdb

from src.sizing.evs import EVSComponents
from src.sizing.plt import (
    CAP_FLOOR,
    CAP_CEILING,
    MIN_SAMPLES_TRUSTED,
    CellKey,
    CellStats,
    exploration_rate,
    features_to_cell,
    lookup_cell,
    total_samples,
)

logger = logging.getLogger("sizing.router")

# Exploration cap range (uniform draw between these when exploring)
EXPLORE_CAP_MIN = 0.10
EXPLORE_CAP_MAX = 0.50  # Slightly lower than CAP_CEILING to avoid wild bets in探索


@dataclass(frozen=True)
class SizingDecision:
    """Final sizing decision with full provenance for decision_shadow."""
    cap_pct: float                  # 0.0 - 1.0 fraction of capital
    source: str                     # "plt_hot" | "plt_warm" | "plt_cold" | "evs_fallback" | "exploration"
    cell_id: str                    # canonical cell identifier
    cell_n_samples: int             # how many trades support this cell
    cell_confidence: str            # "cold" | "warm" | "hot"
    exploration_flag: bool          # True if ε-greedy fired
    fallback_used: bool             # True if PLT was bypassed (cold cell or missing)


def _deterministic_rng(*, ticker: str, decided_date: date) -> random.Random:
    """Per-(ticker, date) seeded RNG for ε-greedy.

    Why deterministic: lets us reproduce sizing decisions exactly when
    auditing. Also avoids "lucky retry"がない (1日内で同銘柄を2回判定して
    違うε draw になることを防ぐ)。
    """
    seed_bytes = f"{ticker}|{decided_date.isoformat()}".encode("utf-8")
    seed_int = int.from_bytes(hashlib.sha256(seed_bytes).digest()[:8], "big")
    return random.Random(seed_int)


def decide_cap(
    *,
    con: duckdb.DuckDBPyConnection,
    components: EVSComponents,
    consensus: int,
    rsi: float,
    nikkei_ma25_dev: Optional[float],
    ticker: str,
    decided_date: date,
    rng: Optional[random.Random] = None,
) -> SizingDecision:
    """Decide final cap_pct using PLT-first dispatch with EVS fallback.

    Args:
        con: DuckDB connection (read access to plt_cells).
        components: EVS output (used for cell binning + fallback cap).
        consensus: 3-5 (gate; should be ≥3 at this point).
        rsi: raw RSI value for bin assignment.
        nikkei_ma25_dev: for regime bin.
        ticker: stock code (for sector + RNG seed).
        decided_date: entry decision date (for RNG seed).
        rng: optional explicit RNG (for testing); default = deterministic per (ticker, date).

    Returns:
        SizingDecision with full provenance.
    """
    cell_key = features_to_cell(
        consensus=consensus,
        dev_depth_score=components.f2_deviation_depth,
        rsi=rsi,
        bb_pen_score=components.f4_bb_penetration,
        nikkei_ma25_dev=nikkei_ma25_dev,
        ticker=ticker,
    )
    cell_id = cell_key.to_id()

    # --- Zero-EVS gate (leak fix 2026-05-22) ---
    # EVS≤0 (信号皆無 or 集中ペナルティ=1) は建てない。
    # 旧バグ: cap_function(0)=CAP_FLOOR(0.10) のため shares_from_decision が
    # evs_total を見ず 10% 配分を承認していた (流動性ゼロ銘柄に資金を入れる)。
    # ここで明示ゲートし cap_pct=0 を返す → shares_from_decision が 0 株にする。
    if components.evs_total <= 0.0:
        return SizingDecision(
            cap_pct=0.0,
            source="evs_zero",
            cell_id=cell_id,
            cell_n_samples=0,
            cell_confidence="cold",
            exploration_flag=False,
            fallback_used=True,
        )

    # PLT lookup
    cell = lookup_cell(con, cell_id)

    # Global n for ε schedule
    global_n = total_samples(con)
    eps = exploration_rate(global_n)

    # RNG for ε-greedy
    if rng is None:
        rng = _deterministic_rng(ticker=ticker, decided_date=decided_date)
    explore_draw = rng.random()

    # --- Dispatch logic ---
    if cell is None:
        # Untouched cell — full EVS fallback, mark as cold
        return SizingDecision(
            cap_pct=components.cap_pct,
            source="evs_fallback",
            cell_id=cell_id,
            cell_n_samples=0,
            cell_confidence="cold",
            exploration_flag=False,
            fallback_used=True,
        )

    if cell.confidence == "cold":
        # Cold cell (n<5): EVS主体だが、探索のため ε で random uniform を試す
        if explore_draw < eps:
            explore_cap = rng.uniform(EXPLORE_CAP_MIN, EXPLORE_CAP_MAX)
            return SizingDecision(
                cap_pct=explore_cap,
                source="exploration",
                cell_id=cell_id,
                cell_n_samples=cell.n_samples,
                cell_confidence="cold",
                exploration_flag=True,
                fallback_used=True,
            )
        return SizingDecision(
            cap_pct=components.cap_pct,
            source="plt_cold",
            cell_id=cell_id,
            cell_n_samples=cell.n_samples,
            cell_confidence="cold",
            exploration_flag=False,
            fallback_used=True,
        )

    # Warm/Hot cell — PLT主軸. ε-greedy で時々 EVS exploration.
    if explore_draw < eps:
        # Exploration: use EVS cap to occasionally challenge PLT's recommendation
        return SizingDecision(
            cap_pct=components.cap_pct,
            source="exploration",
            cell_id=cell_id,
            cell_n_samples=cell.n_samples,
            cell_confidence=cell.confidence,
            exploration_flag=True,
            fallback_used=False,
        )

    # Exploitation: use PLT cell's empirical cap
    return SizingDecision(
        cap_pct=cell.recommended_cap_pct,
        source=f"plt_{cell.confidence}",
        cell_id=cell_id,
        cell_n_samples=cell.n_samples,
        cell_confidence=cell.confidence,
        exploration_flag=False,
        fallback_used=False,
    )


def shares_from_decision(
    *,
    decision: SizingDecision,
    capital: float,
    price: float,
    flex: bool,
) -> tuple[int, str]:
    """Convert a SizingDecision into actual share count.

    Mirrors evs.evs_size logic but uses the router's chosen cap.

    Args:
        decision: from decide_cap()
        capital: book equity
        price: current share price
        flex: True で最低1単元保証 (when cash allows)

    Returns:
        (shares, sizing_reason)
        sizing_reason ∈ {"router_cap", "flex_min_unit", "shares_zero"}
    """
    if capital <= 0 or price <= 0:
        return 0, "shares_zero"

    cap_value = capital * decision.cap_pct
    shares = int(cap_value / price / 100) * 100

    if flex and shares == 0 and capital >= price * 100 and decision.cap_pct > 0:
        # Min-1-unit guarantee, but require some non-zero cap (i.e. EVS or PLT > 0)
        return 100, "flex_min_unit"

    if shares == 0:
        return 0, "shares_zero"

    return shares, "router_cap"
