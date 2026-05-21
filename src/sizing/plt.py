"""Pattern Lookup Table (PLT) — empirical Bayes sizing から学習する成績表.

Phase 0+ (2026-05-21, Grove刷新):
- 6-axis × 432-cell pattern table 上に過去 closed trades の統計を集計
- Cell別 Kelly fraction → cap_pct を動的に算出
- 新規 entry は features → cell_id lookup → cell cap を使用
- Cold cell (n<MIN_SAMPLES_TRUSTED) は EVS連続関数にfallback (router.py)
- 週次 cron で再集計し成績表を進化させる (weekly_retrain.py, Phase 1)

Architecture (Grove 2026-05-21):
- PLT 主軸 / EVS は cold cell fallback
- 10 factors を 6 axis に集約 (連続値→bin化)
- ε=20% → 10% exploration (n依存)

Cell axes (6軸, 432 セル):
- consensus  : {3, 4, 5}                   → 3 bins
- dev_depth  : {[0,0.5], [0.5,1.0]}        → 2 bins (F2 from EVS)
- rsi_band   : {[0,15], [15,25], [25,35]}  → 3 bins
- bb_band    : {[0,0.3], [0.3,1.0]}        → 2 bins
- regime     : {bull, bear}                → 2 bins (P4)
- sector     : {pharma, food, chem, sec, tech, other} → 6 bins
合計: 3 × 2 × 3 × 2 × 2 × 6 = 432 セル

References:
- Walk-Forward Optimization (Pardo 2008)
- Empirical Bayes for sparse cells (Efron & Morris 1973)
- Kelly Criterion + Fractional safety margin (Thorp 1969)
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date, datetime
from typing import NamedTuple, Optional

import duckdb

logger = logging.getLogger("plt")

# ---- Bin definitions (immutable, change only with care) ----

CONSENSUS_BINS = [3, 4, 5]  # consensus values
DEV_DEPTH_BINS = [(0.0, 0.5), (0.5, 2.0)]  # F2 depth ranges
RSI_BANDS = [(0.0, 15.0), (15.0, 25.0), (25.0, 35.0)]  # RSI ranges
BB_BANDS = [(0.0, 0.30), (0.30, 1.0)]  # F4 penetration ranges
REGIME_BINS = ["bull", "bear"]  # P4: nikkei_ma25_dev < 0 → bear
SECTOR_BINS = ["pharma", "food", "chem", "sec", "tech", "other"]

# Ticker → sector mapping (J-Quants 5-digit codes; trailing 0 = 普通株).
# Extends config/sector_config.py with broader coverage for PLT cell density.
# Bins: pharma / food / chem / sec / tech / other (CLAUDE.md固定)
#
# Mapping rationale (J-Quants 5桁 → underlying 4桁 sector group):
# - 45xx: pharma (製薬)
# - 28xx, 29xx: food (食品/バイオ)
# - 40xx-41xx, 50xx (一部), 31xx (繊維材料): chem (化学・材料)
# - 83xx, 86xx: sec (銀行・証券・金融)
# - 67xx, 69xx, 79xx (game), 58xx (電線), 61xx, 36xx, 96xx: tech (電機/IT/半導体)
# - その他 (70xx 自動車/機械, 88xx 不動産, 95xx 電力, 90xx 運輸, 19xx 建設, 8x 小売, ...): other
TICKER_SECTORS: dict[str, str] = {
    # === pharma (-5%) ===
    "45020": "pharma", "45070": "pharma", "45190": "pharma",  # 武田, 塩野義, 中外
    "45680": "pharma", "45230": "pharma", "45520": "pharma",  # 第一三共, エーザイ, JCRファーマ
    # 4-digit legacy
    "4502": "pharma", "4507": "pharma", "4519": "pharma",
    # === food (-7%) ===
    "25020": "food", "28020": "food", "28010": "food",  # アサヒ, 味の素, キッコーマン
    "29140": "food", "29310": "food",                   # JT, ユーグレナ
    "2502": "food", "2802": "food", "2801": "food", "2914": "food",
    # === chem (-7%) ===
    "40630": "chem", "41880": "chem",  # 信越化学, 三菱ケミ
    "50160": "chem", "31100": "chem",  # 新日本理化, 日東紡
    "4063": "chem", "4188": "chem",
    # === sec (-5%, 金融・証券・銀行) ===
    "83060": "sec", "83160": "sec",  # 三菱UFJ, 三井住友
    "86040": "sec",                   # 野村
    "8306": "sec", "8316": "sec", "8604": "sec",
    # === tech (-10%, 電機/IT/半導体/ゲーム) ===
    "69020": "tech", "77510": "tech", "67580": "tech",  # デンソー, キヤノン, ソニー
    "68610": "tech", "99840": "tech",                   # キーエンス, ソフトバンクG
    "67400": "tech", "69200": "tech", "58030": "tech",  # JDI, レーザーテック, フジクラ
    "61460": "tech", "79740": "tech", "96970": "tech",  # ディスコ, 任天堂, カプコン
    "36600": "tech", "36810": "tech",                   # エイチアイ, ブイキューブ
    "6758": "tech", "6861": "tech", "6902": "tech", "7751": "tech", "9984": "tech",
    # === other (機械/自動車/不動産/電力/運輸/建設/小売など) ===
    # NB: "other" は明示する必要なし (default fallback). 統計のため列挙のみ。
}

# ---- Tunable constants ----

# Below this n, cell is considered "cold" and falls back to EVS continuous cap.
MIN_SAMPLES_TRUSTED = 5

# Bayesian shrinkage priors (Beta distribution on win rate).
# Default: Beta(α=5, β=3) → prior mean = 0.625 (slight optimism based on
# aggregate 70.4% winrate of existing 27 closed trades).
SHRINK_PRIOR_WINS = 5.0
SHRINK_PRIOR_LOSSES = 3.0

# Fractional Kelly (safety margin). Quarter Kelly = 0.25 mainstream choice.
KELLY_FRACTION = 0.25

# Cap bounds for PLT-derived cap_pct (same range as EVS fallback).
CAP_FLOOR = 0.10
CAP_CEILING = 0.70

# Default win/loss ratio when sector data is sparse. -5% stoploss + +3% avg win = 0.6.
DEFAULT_B_RATIO = 0.60

# Exploration schedule. ε high when data sparse (encourage exploration).
EXPLORATION_HIGH = 0.20  # When global n < EXPLORATION_TRANSITION_N
EXPLORATION_LOW = 0.10
EXPLORATION_TRANSITION_N = 50


# ---- Data structures ----

class CellKey(NamedTuple):
    """6-axis cell identifier. Used as DB primary key."""
    consensus: int
    dev_bin: int  # 0 = [0, 0.5), 1 = [0.5, 2.0]
    rsi_bin: int  # 0 = [0, 15), 1 = [15, 25), 2 = [25, 35)
    bb_bin: int   # 0 = [0, 0.3), 1 = [0.3, 1.0]
    regime: str   # "bull" | "bear"
    sector: str   # "pharma" | "food" | ...

    def to_id(self) -> str:
        """Stable string ID. Format: c5_d1_r2_b0_bear_tech"""
        return (
            f"c{self.consensus}_d{self.dev_bin}_r{self.rsi_bin}_"
            f"b{self.bb_bin}_{self.regime}_{self.sector}"
        )

    @classmethod
    def from_id(cls, cell_id: str) -> "CellKey":
        """Parse cell_id back to CellKey. Inverse of to_id()."""
        # Format: c{C}_d{D}_r{R}_b{B}_{regime}_{sector}
        parts = cell_id.split("_")
        if len(parts) != 6:
            raise ValueError(f"Invalid cell_id format: {cell_id}")
        c = int(parts[0][1:])
        d = int(parts[1][1:])
        r = int(parts[2][1:])
        b = int(parts[3][1:])
        regime = parts[4]
        sector = parts[5]
        return cls(c, d, r, b, regime, sector)


@dataclass(frozen=True)
class CellStats:
    """Aggregated statistics for a single PLT cell.

    Updated incrementally by bootstrap_plt.py and weekly_retrain.py.
    """
    cell_id: str
    n_samples: int
    n_wins: int
    avg_pnl_pct: float
    std_pnl_pct: float
    avg_win_pct: float       # mean of (exit-entry)/entry for winners
    avg_loss_pct: float      # mean for losers (negative number)
    shrunk_win_rate: float   # Beta posterior mean
    kelly_fraction: float    # f* = (p*b - q)/b with shrinkage
    recommended_cap_pct: float  # KELLY_FRACTION × kelly_fraction, clipped to [CAP_FLOOR, CAP_CEILING]
    last_updated: datetime
    confidence: str          # "cold" (n<5), "warm" (5≤n<20), "hot" (n≥20)


# ---- Bin assignment ----

def assign_dev_bin(dev_depth_score: float) -> int:
    """F2 deviation_depth (∈ [0,1]) → bin index."""
    return 0 if dev_depth_score < 0.5 else 1


def assign_rsi_bin(rsi: float) -> int:
    """RSI value → bin index. RSI≥35 → 2 (low-confidence高bin)."""
    if rsi < 15.0:
        return 0
    if rsi < 25.0:
        return 1
    return 2


def assign_bb_bin(bb_pen_score: float) -> int:
    """F4 bb_penetration (∈ [0,1]) → bin index."""
    return 0 if bb_pen_score < 0.30 else 1


def assign_regime(nikkei_ma25_dev: Optional[float]) -> str:
    """P4 logic: nikkei < MA25 (dev<0) → bear, else bull."""
    if nikkei_ma25_dev is None:
        return "bull"  # conservative default
    return "bear" if nikkei_ma25_dev < 0 else "bull"


def assign_sector(ticker: str) -> str:
    """Ticker → sector bin. Fallback to 'other' for unmapped."""
    return TICKER_SECTORS.get(ticker, "other")


def features_to_cell(
    *,
    consensus: int,
    dev_depth_score: float,
    rsi: float,
    bb_pen_score: float,
    nikkei_ma25_dev: Optional[float],
    ticker: str,
) -> CellKey:
    """Map raw features (post-EVS) to canonical CellKey.

    Args:
        consensus: 3, 4, or 5. Values below 3 should not reach here (gated upstream).
        dev_depth_score: F2 from EVS (∈ [0, 1]).
        rsi: raw RSI value.
        bb_pen_score: F4 from EVS (∈ [0, 1]).
        nikkei_ma25_dev: market regime input.
        ticker: stock code (used for sector lookup).
    """
    return CellKey(
        consensus=max(3, min(5, consensus)),  # clip to valid range
        dev_bin=assign_dev_bin(dev_depth_score),
        rsi_bin=assign_rsi_bin(rsi),
        bb_bin=assign_bb_bin(bb_pen_score),
        regime=assign_regime(nikkei_ma25_dev),
        sector=assign_sector(ticker),
    )


# ---- Empirical Bayes aggregation ----

def shrunk_win_rate(
    wins: int,
    losses: int,
    prior_wins: float = SHRINK_PRIOR_WINS,
    prior_losses: float = SHRINK_PRIOR_LOSSES,
) -> float:
    """Beta posterior mean of win rate, given Beta(α, β) prior.

    Identical to evs.bayes_winrate but exposed here for explicit usage in PLT.
    """
    return (wins + prior_wins) / (wins + losses + prior_wins + prior_losses)


def kelly_fraction(
    win_rate: float,
    avg_win_pct: float,
    avg_loss_pct: float,
) -> float:
    """Kelly Criterion: f* = (p*b - q) / b, clipped to [0, 1].

    Args:
        win_rate: p (after shrinkage).
        avg_win_pct: mean win as positive fraction (e.g., 0.03).
        avg_loss_pct: mean loss as negative fraction (e.g., -0.05). Will be made positive.
    Returns:
        Full Kelly fraction. Caller applies KELLY_FRACTION safety margin.
    """
    if avg_win_pct <= 0 or avg_loss_pct >= 0:
        # Insufficient data for ratio
        b = DEFAULT_B_RATIO
    else:
        b = avg_win_pct / abs(avg_loss_pct)

    if b <= 0:
        return 0.0
    q = 1.0 - win_rate
    f = (win_rate * b - q) / b
    return max(0.0, min(f, 1.0))


def cap_from_kelly(kelly_f: float) -> float:
    """Apply fractional Kelly safety + cap bounds."""
    scaled = kelly_f * KELLY_FRACTION
    return max(CAP_FLOOR, min(scaled, CAP_CEILING))


def aggregate_cell(
    cell_id: str,
    pnl_pcts: list[float],
    *,
    now: Optional[datetime] = None,
) -> CellStats:
    """Compute CellStats from a list of pnl% observations.

    Args:
        cell_id: Stable cell identifier.
        pnl_pcts: list of (exit-entry)/entry decimals (positive = win, negative = loss).
    """
    if now is None:
        now = datetime.now()
    n = len(pnl_pcts)
    if n == 0:
        # Cold cell with no data — uses pure prior for shrinkage.
        return CellStats(
            cell_id=cell_id, n_samples=0, n_wins=0,
            avg_pnl_pct=0.0, std_pnl_pct=0.0,
            avg_win_pct=0.0, avg_loss_pct=0.0,
            shrunk_win_rate=shrunk_win_rate(0, 0),
            kelly_fraction=0.0,
            recommended_cap_pct=CAP_FLOOR,
            last_updated=now,
            confidence="cold",
        )

    wins = [p for p in pnl_pcts if p > 0]
    losses = [p for p in pnl_pcts if p <= 0]
    n_win = len(wins)
    n_loss = len(losses)
    avg_pnl = sum(pnl_pcts) / n
    var = sum((p - avg_pnl) ** 2 for p in pnl_pcts) / n if n > 1 else 0.0
    std_pnl = var ** 0.5

    avg_win = sum(wins) / n_win if n_win > 0 else 0.0
    avg_loss = sum(losses) / n_loss if n_loss > 0 else 0.0

    p_shrunk = shrunk_win_rate(n_win, n_loss)
    k_f = kelly_fraction(p_shrunk, avg_win, avg_loss)
    cap = cap_from_kelly(k_f)

    confidence = "cold" if n < MIN_SAMPLES_TRUSTED else ("warm" if n < 20 else "hot")

    return CellStats(
        cell_id=cell_id,
        n_samples=n,
        n_wins=n_win,
        avg_pnl_pct=avg_pnl,
        std_pnl_pct=std_pnl,
        avg_win_pct=avg_win,
        avg_loss_pct=avg_loss,
        shrunk_win_rate=p_shrunk,
        kelly_fraction=k_f,
        recommended_cap_pct=cap,
        last_updated=now,
        confidence=confidence,
    )


def exploration_rate(global_n: int) -> float:
    """ε-greedy schedule: high when data sparse, decays as global n grows.

    Grove (2026-05-21): ε=20% (n<50) → ε=10% (n≥50)
    """
    return EXPLORATION_HIGH if global_n < EXPLORATION_TRANSITION_N else EXPLORATION_LOW


# ---- Persistence (DuckDB) ----

PLT_TABLE_DDL = """
CREATE TABLE IF NOT EXISTS plt_cells (
    cell_id        VARCHAR PRIMARY KEY,
    n_samples      INTEGER NOT NULL DEFAULT 0,
    n_wins         INTEGER NOT NULL DEFAULT 0,
    avg_pnl_pct    DOUBLE  NOT NULL DEFAULT 0.0,
    std_pnl_pct    DOUBLE  NOT NULL DEFAULT 0.0,
    avg_win_pct    DOUBLE  NOT NULL DEFAULT 0.0,
    avg_loss_pct   DOUBLE  NOT NULL DEFAULT 0.0,
    shrunk_win_rate DOUBLE NOT NULL DEFAULT 0.625,
    kelly_fraction DOUBLE  NOT NULL DEFAULT 0.0,
    recommended_cap_pct DOUBLE NOT NULL DEFAULT 0.10,
    last_updated   TIMESTAMP NOT NULL,
    confidence     VARCHAR NOT NULL DEFAULT 'cold'
)
"""


def ensure_plt_table(con: duckdb.DuckDBPyConnection) -> None:
    """Create plt_cells table if not exists."""
    con.execute(PLT_TABLE_DDL)


def upsert_cell(con: duckdb.DuckDBPyConnection, stats: CellStats) -> None:
    """Insert or update a single cell's stats."""
    ensure_plt_table(con)
    con.execute(
        """
        INSERT INTO plt_cells (cell_id, n_samples, n_wins, avg_pnl_pct, std_pnl_pct,
                                avg_win_pct, avg_loss_pct, shrunk_win_rate,
                                kelly_fraction, recommended_cap_pct, last_updated, confidence)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT (cell_id) DO UPDATE SET
            n_samples = excluded.n_samples,
            n_wins = excluded.n_wins,
            avg_pnl_pct = excluded.avg_pnl_pct,
            std_pnl_pct = excluded.std_pnl_pct,
            avg_win_pct = excluded.avg_win_pct,
            avg_loss_pct = excluded.avg_loss_pct,
            shrunk_win_rate = excluded.shrunk_win_rate,
            kelly_fraction = excluded.kelly_fraction,
            recommended_cap_pct = excluded.recommended_cap_pct,
            last_updated = excluded.last_updated,
            confidence = excluded.confidence
        """,
        [
            stats.cell_id, stats.n_samples, stats.n_wins,
            stats.avg_pnl_pct, stats.std_pnl_pct,
            stats.avg_win_pct, stats.avg_loss_pct,
            stats.shrunk_win_rate, stats.kelly_fraction,
            stats.recommended_cap_pct, stats.last_updated, stats.confidence,
        ],
    )


def lookup_cell(
    con: duckdb.DuckDBPyConnection,
    cell_id: str,
) -> Optional[CellStats]:
    """Retrieve a cell's stats from DB. Returns None if not exists."""
    ensure_plt_table(con)
    row = con.execute(
        """
        SELECT cell_id, n_samples, n_wins, avg_pnl_pct, std_pnl_pct,
               avg_win_pct, avg_loss_pct, shrunk_win_rate, kelly_fraction,
               recommended_cap_pct, last_updated, confidence
        FROM plt_cells WHERE cell_id = ?
        """,
        [cell_id],
    ).fetchone()
    if row is None:
        return None
    return CellStats(
        cell_id=row[0], n_samples=row[1], n_wins=row[2],
        avg_pnl_pct=row[3], std_pnl_pct=row[4],
        avg_win_pct=row[5], avg_loss_pct=row[6],
        shrunk_win_rate=row[7], kelly_fraction=row[8],
        recommended_cap_pct=row[9],
        last_updated=row[10], confidence=row[11],
    )


def total_samples(con: duckdb.DuckDBPyConnection) -> int:
    """Sum of n_samples across all cells. Used for global exploration schedule."""
    ensure_plt_table(con)
    row = con.execute("SELECT COALESCE(SUM(n_samples), 0) FROM plt_cells").fetchone()
    return int(row[0]) if row else 0
