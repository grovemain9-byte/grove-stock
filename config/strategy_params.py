"""S1: 戦略パラメータの単一ソース（plan: config集約 Lane A）。

現状 20軸がコード各所にハードコード分散（engine.py/voting.py/monitor.py/
main.py/kelly.py/runner.py/universe.py）。本モジュールは**現値の写し**を
1箇所に集約する単一スキーマ。S1では未配線（誰も import しない＝挙動ゼロ影響）。
S2(backtest)→S3(live) で各モジュールが DEFAULTS を参照するよう配線し、
test_characterization.py が緑のままなら「集約は挙動保存」を機械的に証明。

設計判断（plan準拠）:
- pydantic不採用 = 新規依存回避・repo既存の @dataclass(frozen=True) 流儀
- kelly_alloc は mutable default 罠回避のため tuple-of-tuples（真の不変）。
  dict が要る箇所は kelly_alloc_map() で取得
- sector_threshold_mode は Lane B 用フィールド（既定 "ksigma" 固定、将来③で
  council が触れる余地。S6 で配線）
- books（ペーパー資金ブック）も単一ソース化のため参照を保持
"""
from __future__ import annotations

from dataclasses import dataclass

from config.books import BOOKS, Book


@dataclass(frozen=True)
class StrategyParams:
    # --- 指標 (engine.py / players) ---
    ma_period: int = 25
    rsi_period: int = 14
    rsi_threshold: float = 35.0
    bb_period: int = 25
    bb_std: float = 2.0
    p5_vol_lookback: int = 3
    # --- レジーム (P4) ---
    p4_threshold: float = 0.0
    p4_required: bool = False  # 2026-05-12 grid: 必須解除（baseline はこれ）
    # 2026-05-20: 本田MTG bridge — 銘柄別 EMA900 上昇ゲート（既定 False=従来挙動 exact）
    per_stock_uptrend_required: bool = False
    # --- コンセンサス/出口 (voting/monitor/engine) ---
    consensus_min: int = 3
    stop_loss: float = -0.07
    max_hold_days: int = 15
    # 2026-05-25 令和式 BNF exit層改修:
    # - take_profit_dev 0.0 → 0.02: MA25回帰でなく +2%乖離プラスで利確 (Honda asymmetric_tp)
    # - take_profit priority を signal_reversal より前 に格上げ (旧: signal_reversal が先勝ち
    #   で take_profit 0件発火 だった病巣修正)
    # - trailing_stop: +1.5%到達後、pick から -1% 下落で exit (max +2.69% avg 捕捉)
    # - gap_down_stop: 寄付き値が entry の -3% 以下で即exit (-7%設計stop_loss が gap で
    #   -7~-8.5% にずれる問題を緩和)
    take_profit_dev: float = 0.02  # deviation >= +0.02 で利確 (旧 0.0 / Honda asymmetric_tp)
    trailing_activation_pct: float = 0.015  # +1.5% touched で trailing 活性化
    trailing_drawdown_pct: float = 0.01     # peak から -1% で exit
    gap_down_stop_pct: float = -0.03        # 寄付きが entry-3% 以下で即exit
    max_concurrent: int = 7
    # --- Kelly 配分 (kelly.py) tuple-of-tuples=不変 ---
    kelly_alloc: tuple[tuple[int, float], ...] = ((3, 0.10), (4, 0.15), (5, 0.20))
    # --- セクター閾値 (runner.compute_sector_thresholds_from_cache) ---
    sector_k: float = 2.0
    th_floor: float = -0.20
    th_ceiling: float = -0.03
    sector_threshold_mode: str = "ksigma"  # Lane B: "ksigma" | "static"
    default_sector_th: float = -0.07  # static時/未登録時のフォールバック
    # --- ユニバース (universe.py / S7で配線) ---
    universe_market: tuple[str, ...] = ("Prime",)
    universe_scale: tuple[str, ...] | None = None  # None = ScaleCat絞りなし
    adv_min_jpy: float = 1e8
    # --- ペーパー資金ブック (Phase 0 / S1で単一ソース化) ---
    books: tuple[Book, ...] = BOOKS

    def kelly_alloc_map(self) -> dict[int, float]:
        """consensus→配分率 の dict（kelly.py 互換形）。"""
        return {c: a for c, a in self.kelly_alloc}


DEFAULTS = StrategyParams()
