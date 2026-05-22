"""Expected Value Score (EVS) — multi-factor scoring for entry sizing.

Phase 0+ (2026-05-21, Grove刷新後):
- 10 factors (F1-F10) 完備
- Grove方針: 「1-10段階で最大利益・最大効率」「成績表で動的に動け」
- v2 architecture: PLT主軸 + EVS は cold cell fallback (src/sizing/router.py)

Factors:
- F1  signal_strength            (consensus / 5, with 5/5 bonus)
- F2  deviation_depth            (|MA25 dev| / |sector threshold|, clipped at 2x)
- F3  rsi_oversold               ((35 - rsi) / 35, deeper = stronger signal)
- F4  bb_penetration             ((bb_lower - close) / bb_lower × 50, capped)
- F5  volume_decline_intensity   ((v[-3] - v[-1]) / v[-3], 売り圧力収束度)
- F6  bayes_winrate              (Beta(α=5, β=3) posterior on sector closed positions)
- F7  market_regime              (nikkei < MA25 → 1.0, else 0.7)
- F8  realized_vol_filter        (1 - clip(vol_20d / 0.50, 0, 1), 過剰ボラpenalty)
- F9  concentration_penalty      (same ticker = 1.0, same sector = 0.20 each)
- F10 liquidity_filter           (log(avg_volume_20d) / 14, 流動性 score)

Weights (Phase 0 initial; Phase 1 で scipy.optimize で walk-forward 最適化):
- F1=0.20, F2=0.20, F3=0.10, F4=0.10, F5=0.10 (signal layer)
- F6, F7, F8, F9, F10 は乗算式 (multiplicative penalty/boost layer)

Cap curve (Phase 0 fallback): linear cap = 0.10 + 0.60 × EVS  → [10%, 70%]
※ Phase 0+ では PLT (Pattern Lookup Table) のセル別 cap を主軸とし、
   EVS連続関数は PLT cold cell (n<5) の fallback として使用。

Phase 1 (来週・n≥40 trades): scipy.optimize で weights を walk_forward Sharpe最大化
Phase 2 (1ヶ月後・n≥100): PLT 全セル更新、ε-greedy router 安定運用
Phase 3 (3ヶ月後・n≥300): LightGBM EV regression 並走

Reference: docs/grove-stock/build_diary.md 2026-05-21 entry.
"""
from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Optional

logger = logging.getLogger("sizing.evs")


# --- Tunable constants (Phase 0+; Phase 1 で data/evs_weights.json で上書き) ---

# Default signal layer weights (additive). Phase 0+ initial (Grove 2026-05-21).
# Phase 1 で scipy.optimize の出力 (data/evs_weights.json) が読み込まれて上書き。
# All-additive 9-factor weighting (Grove 2026-05-22 刷新後).
# 旧構造 (F1-F5 加重和 × F6·F7·F8·F10 乗算) は prior_adj median=0.09 に崩壊した
# (4要素乗算の AND 的性質)。Grove方針「可能性を消すな・スコアでサイズ」に基づき
# 全 9 factor を1つの加重和に統合。F9(集中) のみ (1-penalty) 乗算ペナルティとして残す。
# F10(流動性) は paper ではスコア (建てる)、live では参加率ゲートに昇格 (別途実装)。
# 重みは data/evs_weights.json で動的上書き、5/30 scipy 最適化で学習。
DEFAULT_WEIGHTS: dict[str, float] = {
    # Signal layer (BNF逆張りの core edge)
    "F1": 0.18,   # consensus
    "F2": 0.18,   # deviation depth
    "F3": 0.10,   # rsi
    "F4": 0.10,   # bb penetration
    "F5": 0.08,   # volume decline
    # Quality layer (旧 multiplicative prior — additive に格下げで崩壊回避)
    "F6": 0.16,   # bayes winrate
    "F7": 0.08,   # market regime
    "F8": 0.06,   # realized vol filter (Grove W2: 残す、5/30検証)
    "F10": 0.06,  # liquidity (paper=score)
}  # sum = 1.00

# Module-level WEIGHTS (mutable): overridden by data/evs_weights.json if exists.
# Path resolved from env var EVS_WEIGHTS_PATH or default location.
_DEFAULT_WEIGHTS_PATH = Path(__file__).resolve().parents[2] / "data" / "evs_weights.json"


def _load_weights_from_json(path: Path) -> Optional[dict[str, float]]:
    """Load weights from JSON file. Returns None if missing/invalid."""
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text())
        weights = payload.get("weights")
        if not isinstance(weights, dict):
            return None
        # Validate expected keys
        out = {}
        for k in ["F1", "F2", "F3", "F4", "F5"]:
            v = weights.get(k)
            if v is None or not isinstance(v, (int, float)):
                return None
            out[k] = float(v)
        # Sanity check: non-negative and not all-zero
        if any(v < 0 for v in out.values()) or sum(out.values()) <= 0:
            return None
        return out
    except (json.JSONDecodeError, OSError, KeyError, TypeError):
        return None


def _resolve_weights() -> dict[str, float]:
    """Resolve WEIGHTS: env override > JSON file > DEFAULT_WEIGHTS."""
    env_path = os.getenv("EVS_WEIGHTS_PATH")
    path = Path(env_path) if env_path else _DEFAULT_WEIGHTS_PATH
    loaded = _load_weights_from_json(path)
    if loaded is not None:
        logger.info("Loaded EVS weights from %s: %s", path, loaded)
        return loaded
    return dict(DEFAULT_WEIGHTS)


# Active weights (resolved at import). Re-resolved via reload_weights() if needed.
WEIGHTS: dict[str, float] = _resolve_weights()


def reload_weights() -> dict[str, float]:
    """Re-resolve and apply weights. Returns the new active weights.

    Call after weekly_retrain.py writes a new evs_weights.json without restarting
    the long-running process (e.g., LangGraph daemon).
    """
    global WEIGHTS
    WEIGHTS = _resolve_weights()
    return WEIGHTS

# Cap curve: cap = CAP_FLOOR + CAP_CEILING_DELTA × EVS, range [2%, 70%].
# CAP_FLOOR 引下げ 0.10→0.02 (Grove W5): 真の floor は「1単元保証(株数ベース)」であり
# % floor ではない。EVS≤0 は router でゲート (0株) するため、% floor は EVS>0 のみ適用。
CAP_FLOOR = 0.02            # EVS→0+ → 2% (1単元保証が実質下限)
CAP_CEILING_DELTA = 0.68    # EVS=1 → 70% of capital

# Bayesian shrinkage priors (Beta-Bernoulli).
# Phase 0: optimistic prior reflecting all-book aggregate of 70.4% winrate.
BAYES_PRIOR_WINS = 5.0
BAYES_PRIOR_LOSSES = 3.0    # E[p] = 5/8 = 0.625

# Concentration penalty coefficients
SAME_TICKER_PENALTY = 1.0   # Full block (already_open route still applies)
SAME_SECTOR_PENALTY = 0.20  # Per same-sector position held
SAME_SECTOR_CAP = 0.80      # Penalty cannot exceed 0.80 (keeps EVS > 0)

# F2 deviation_depth clipping (2x threshold breach → 100% credit)
DEVIATION_CLIP = 2.0

# F5 volume_decline clip — v[-3] vs v[-1] ratio fully credit at 50% decline
VOLUME_DECLINE_CLIP = 0.50

# F8 realized_vol filter — full penalty when 20d annualized vol > 50%
VOL_FILTER_CEILING = 0.50

# F10 liquidity floor — log(avg_volume_20d) normalized by typical Tokyo大型株 log≈14
LIQUIDITY_LOG_CEILING = 14.0


# --- Data classes ---

@dataclass(frozen=True)
class EVSComponents:
    """Decomposed factors for diagnostics + decision_shadow logging."""
    # Signal layer (additive, weighted)
    f1_signal_strength: float
    f2_deviation_depth: float
    f3_rsi_oversold: float
    f4_bb_penetration: float
    f5_volume_decline: float
    # Quality layer (additive since 2026-05-22; F9 is the only penalty)
    f6_bayes_winrate: float
    f7_market_regime: float
    f8_realized_vol_filter: float
    f9_concentration_penalty: float
    f10_liquidity_filter: float
    # Composites
    edge_score: float          # signal-only sub-score (weighted F1..F5, diagnostic)
    prior_adj: float           # legacy F6·F7·F8·F10 multiplicative (diagnostic only)
    evs_total: float           # score(all 9 additive) × (1 - F9)
    cap_pct: float             # fallback linear cap (used only when PLT cold)

    def as_dict(self) -> dict:
        """JSON-serializable for decision_shadow logging."""
        return {k: round(v, 4) for k, v in asdict(self).items()}


# --- Factor functions (pure, testable independently) ---

def signal_strength(consensus: int, max_consensus: int = 5) -> float:
    """F1: normalized consensus + 5/5 bonus, capped at 1.0.

    consensus=3 → 0.60, consensus=4 → 0.80, consensus=5 → 1.00 (with bonus).
    """
    if consensus <= 0:
        return 0.0
    base = min(consensus / max_consensus, 1.0)
    bonus = 0.20 if consensus >= max_consensus else 0.0
    return min(base + bonus, 1.0)


def deviation_depth(actual_dev: float, threshold: float) -> float:
    """F2: how deeply MA25 deviation breached the sector threshold.

    Args:
        actual_dev: signed deviation, e.g., -0.10 for -10%.
        threshold: signed threshold, e.g., -0.07 for -7%.

    Returns:
        0.0 if no breach. 0.5 at threshold. 1.0 at 2x threshold (capped).
    """
    if threshold >= 0 or actual_dev >= 0:
        return 0.0
    ratio = abs(actual_dev) / abs(threshold)
    return min(ratio, DEVIATION_CLIP) / DEVIATION_CLIP


def rsi_oversold(rsi: float, threshold: float = 35.0) -> float:
    """F3: depth below RSI threshold. rsi=35→0, rsi=0→1.0."""
    if rsi >= threshold or threshold <= 0:
        return 0.0
    return min((threshold - rsi) / threshold, 1.0)


def bb_penetration(close: float, bb_lower: float) -> float:
    """F4: penetration depth below Bollinger lower band.

    close at bb_lower → 0; close 2% below bb_lower → 1.0 (capped).
    """
    if bb_lower <= 0 or close >= bb_lower:
        return 0.0
    pct_below = (bb_lower - close) / bb_lower
    return min(pct_below * 50.0, 1.0)


def volume_decline_intensity(v_m3: float, v_m2: float, v_m1: float) -> float:
    """F5: 出来高減少強度 = max(0, (v[-3] - v[-1]) / v[-3]).

    P5 既存定義 (v[-3] > v[-2] > v[-1]) を連続値化。
    Args:
        v_m3, v_m2, v_m1: 直近 -3, -2, -1 営業日の出来高。
    Returns:
        0 (減少なし) ～ 1 (50%以上減少, capped). 順序違反 (v_m1>v_m3) は 0。
    """
    if v_m3 <= 0:
        return 0.0
    # Monotonic check: 順序が崩れていたら大幅減点
    monotonic = v_m3 > v_m2 > v_m1
    decline = (v_m3 - v_m1) / v_m3
    if decline <= 0:
        return 0.0
    score = min(decline / VOLUME_DECLINE_CLIP, 1.0)
    # Monotonic 不一致なら 0.5 倍 (シグナル弱体化、完全棄却ではない)
    return score if monotonic else score * 0.5


def realized_vol_filter(vol_20d_annualized: Optional[float]) -> float:
    """F8: 実現ボラフィルタ. 過剰ボラ銘柄は減点 (1.0 → 0.0).

    Args:
        vol_20d_annualized: 20日リターン std × sqrt(252). 0.25 = 25%/年。
    Returns:
        1.0 (vol=0) ～ 0.0 (vol≥50%, ceiling). None なら 0.85 (uncertainty)。
    """
    if vol_20d_annualized is None:
        return 0.85
    if vol_20d_annualized <= 0:
        return 1.0
    return max(1.0 - min(vol_20d_annualized / VOL_FILTER_CEILING, 1.0), 0.0)


def liquidity_filter(avg_volume_20d: Optional[float]) -> float:
    """F10: 流動性フィルタ. log(avg_volume) を [0, 1] 正規化.

    流動性 ≈ exp(7) (1100株/日) → 0.5、exp(14) (1.2M株/日) → 1.0 (大型株)。
    薄商い銘柄 (avg < 1000) はスリッページ大の予兆として減点。

    Args:
        avg_volume_20d: 20日平均出来高 (株数)。
    Returns:
        0.0 (極薄商い) ～ 1.0 (高流動).
    """
    if avg_volume_20d is None or avg_volume_20d <= 1.0:
        return 0.0
    import math
    return min(math.log(avg_volume_20d) / LIQUIDITY_LOG_CEILING, 1.0)


def bayes_winrate(
    wins: int,
    losses: int,
    prior_wins: float = BAYES_PRIOR_WINS,
    prior_losses: float = BAYES_PRIOR_LOSSES,
) -> float:
    """F6: Bayesian posterior mean win rate (Beta-Bernoulli).

    With Beta(5, 3) prior: 0 samples → 0.625, n=10 wins/0 losses → 0.833,
    n=0/10 → 0.278. Naturally shrinks low-sample estimates toward prior.
    """
    return (wins + prior_wins) / (wins + losses + prior_wins + prior_losses)


def market_regime(nikkei_ma25_dev: Optional[float]) -> float:
    """F7: bearish market = 1.0 (BNF逆張り setup), bullish = 0.7 (penalty).

    None (data missing) → 0.85 (uncertainty discount, between bull/bear).
    """
    if nikkei_ma25_dev is None:
        return 0.85
    return 1.0 if nikkei_ma25_dev < 0 else 0.7


def concentration_penalty(
    target_ticker: str,
    target_sector: str,
    held_tickers: set[str],
    held_sectors: dict[str, int],
) -> float:
    """F9: portfolio concentration penalty in [0, SAME_SECTOR_CAP].

    Same ticker held → 1.0 (full block; should also be caught by already_open route).
    Per same-sector position → +0.20, capped at 0.80.
    """
    if target_ticker in held_tickers:
        return SAME_TICKER_PENALTY
    same_sector_n = held_sectors.get(target_sector, 0)
    return min(same_sector_n * SAME_SECTOR_PENALTY, SAME_SECTOR_CAP)


def cap_function(evs: float) -> float:
    """Linear cap curve: cap_pct = floor + delta × EVS, clipped to [floor, floor+delta]."""
    evs_clipped = max(0.0, min(evs, 1.0))
    return CAP_FLOOR + CAP_CEILING_DELTA * evs_clipped


# --- Composition ---

def compute_evs(
    *,
    consensus: int,
    actual_dev: Optional[float],
    sector_threshold: Optional[float],
    rsi: Optional[float],
    close: Optional[float],
    bb_lower: Optional[float],
    volumes_3day: Optional[tuple[float, float, float]],  # (v[-3], v[-2], v[-1])
    sector_wins: int,
    sector_losses: int,
    nikkei_ma25_dev: Optional[float],
    realized_vol_20d: Optional[float],
    avg_volume_20d: Optional[float],
    target_ticker: str,
    target_sector: str,
    held_tickers: set[str],
    held_sectors: dict[str, int],
) -> EVSComponents:
    """Compute full 10-factor EVS pipeline.

    Robust to missing inputs: None values degrade the corresponding factor to
    a neutral value (0 for signal-side, 0.85 for prior-side uncertainty).
    This preserves entry option but penalizes low-information setups.
    """
    # Signal layer (F1-F5)
    f1 = signal_strength(consensus)
    f2 = (deviation_depth(actual_dev, sector_threshold)
          if (actual_dev is not None and sector_threshold is not None) else 0.0)
    f3 = rsi_oversold(rsi) if rsi is not None else 0.0
    f4 = (bb_penetration(close, bb_lower)
          if (close is not None and bb_lower is not None) else 0.0)
    f5 = (volume_decline_intensity(*volumes_3day)
          if volumes_3day is not None and all(v is not None for v in volumes_3day)
          else 0.0)

    # Quality layer (F6-F8, F10) — additive (旧乗算 prior_adj 崩壊を回避)
    f6 = bayes_winrate(sector_wins, sector_losses)
    f7 = market_regime(nikkei_ma25_dev)
    f8 = realized_vol_filter(realized_vol_20d)
    f10 = liquidity_filter(avg_volume_20d)

    # Concentration (F9) — 唯一の乗算ペナルティとして残す (1-F9)
    f9 = concentration_penalty(target_ticker, target_sector, held_tickers, held_sectors)

    # --- All-additive composition (Grove 2026-05-22) ---
    # 9 factor を1つの加重和に。1要素が低くても全崩壊しない (旧乗算の AND 性質を解消)。
    w_sum = sum(WEIGHTS.values())
    factor_vals = {
        "F1": f1, "F2": f2, "F3": f3, "F4": f4, "F5": f5,
        "F6": f6, "F7": f7, "F8": f8, "F10": f10,
    }
    score = sum(WEIGHTS[k] * v for k, v in factor_vals.items()) / w_sum
    # edge_score = signal-only サブスコア (診断用に保持)
    sig_w = WEIGHTS["F1"] + WEIGHTS["F2"] + WEIGHTS["F3"] + WEIGHTS["F4"] + WEIGHTS["F5"]
    edge_score = (
        WEIGHTS["F1"] * f1 + WEIGHTS["F2"] * f2 + WEIGHTS["F3"] * f3
        + WEIGHTS["F4"] * f4 + WEIGHTS["F5"] * f5
    ) / sig_w if sig_w > 0 else 0.0
    # prior_adj は旧乗算を診断用に残すが evs_total には使わない (後方互換 logging)
    prior_adj = f6 * f7 * f8 * f10
    # 集中ペナルティのみ乗算
    evs_total = score * (1.0 - f9)
    cap_pct = cap_function(evs_total)

    return EVSComponents(
        f1_signal_strength=f1,
        f2_deviation_depth=f2,
        f3_rsi_oversold=f3,
        f4_bb_penetration=f4,
        f5_volume_decline=f5,
        f6_bayes_winrate=f6,
        f7_market_regime=f7,
        f8_realized_vol_filter=f8,
        f9_concentration_penalty=f9,
        f10_liquidity_filter=f10,
        edge_score=edge_score,
        prior_adj=prior_adj,
        evs_total=evs_total,
        cap_pct=cap_pct,
    )


def evs_size(
    *,
    components: EVSComponents,
    capital: float,
    price: float,
    flex: bool,
    consensus: int,
    consensus_threshold: int = 3,
) -> tuple[int, str]:
    """Determine entry size (shares, 100-unit rounded) using EVS cap.

    Args:
        components: EVS pipeline output.
        capital: book equity for sizing basis.
        price: current price per share.
        flex: if True, guarantee min 1 unit (100 shares) when cash allows AND
              EVS > 0 (i.e. at least some edge).
        consensus: original consensus count (gate).
        consensus_threshold: minimum consensus to consider (default 3).

    Returns:
        (shares, cap_reason)
        cap_reason ∈ {"evs_zero", "consensus_low", "shares_zero",
                      "flex_min_unit", "evs_cap"}

    Notes:
        - This is the *book-internal* sizing decision. Caller (kelly_node)
          must additionally enforce cash cap and portfolio slot limit.
        - flex_min_unit is the documented escape hatch for small books with
          expensive stocks. EVS must still be > 0 for it to fire.
    """
    if consensus < consensus_threshold:
        return 0, "consensus_low"
    if components.evs_total <= 0.0 or capital <= 0 or price <= 0:
        return 0, "evs_zero"

    cap_value = capital * components.cap_pct
    shares = int(cap_value / price / 100) * 100

    if flex and shares == 0 and capital >= price * 100:
        # 最低1単元保証 — Grove方針: 「期待値高なら枷外して entry」
        # EVS > 0 を要件にすることで「全シグナル弱でも 1 単元突っ込む」は防ぐ
        return 100, "flex_min_unit"

    if shares == 0:
        return 0, "shares_zero"

    return shares, "evs_cap"
