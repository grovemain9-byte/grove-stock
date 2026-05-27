"""grove-stock DuckDB スキーマ管理。

Issue #1: BNF戦略用の3テーブル（scans / positions / monthly_pnl）。
Q学習・Reflexion関連は廃止済み。
"""
from __future__ import annotations

import logging
import time
from pathlib import Path

import duckdb

logger = logging.getLogger("data.db")

DEFAULT_DB_PATH = Path(__file__).resolve().parent.parent.parent / "data" / "grove_stock.duckdb"

# DuckDBは「単一RW or 複数RO」のみ許容。cron重複(scan×3 + daily_update)で
# "Could not set lock on file" が頻発し scan/daily_update が落ちていた。
# 指数バックオフでリトライし、ロック解放を待つ（プロセス短命なので数秒で空く）。
_LOCK_RETRIES = 6
_LOCK_BASE_DELAY = 0.4


def connect_duckdb(
    path: str | Path,
    *,
    read_only: bool = False,
    retries: int = _LOCK_RETRIES,
    base_delay: float = _LOCK_BASE_DELAY,
) -> duckdb.DuckDBPyConnection:
    """ロック競合に強いDuckDB接続。lock取得失敗時のみ指数バックオフで再試行。"""
    last: Exception | None = None
    for attempt in range(retries):
        try:
            return duckdb.connect(str(path), read_only=read_only)
        except (duckdb.IOException, duckdb.Error) as e:
            msg = str(e).lower()
            if "lock" not in msg and "could not set lock" not in msg:
                raise
            last = e
            wait = base_delay * (2 ** attempt)
            logger.warning("duckdb lock on %s (attempt %d/%d), retry in %.1fs",
                            path, attempt + 1, retries, wait)
            time.sleep(wait)
    raise RuntimeError(f"duckdb lock not released after {retries} retries: {last}")

VALID_STATUS = ("open", "closed")
VALID_EXIT_REASON = ("stop_loss", "max_hold", "take_profit", "signal_reversal", "news_negative")

COMMISSION_RATE = 0.0011  # 0.1% + 消費税10%
COMMISSION_MIN = 110.0    # 最低手数料（税込）


def calc_commission(price: float, shares: int) -> float:
    """片道手数料 (税込).

    2026-05-27 切替: 立花e支店 (0.1%+10%税, min¥110) → kabu (Mitsubishi UFJ eSmart) SOR条件 ¥0.
    令和式 BNF era (2026-05-17〜) は kabu commission ¥0 想定で paper 運用。
    立花は内部 broker API 動作のみ継続、commission モデルは kabu 想定に切替。

    過去 (5/12-5/16) の commission データは legacy 立花値 のまま (歴史記録)、
    5/17以降は ¥0 (DB migration 2026-05-27 で適用済)。
    """
    return 0.0


def get_book_hwm(con: duckdb.DuckDBPyConnection, book: str, initial_capital: float) -> float:
    """book の HWM (High-Water-Mark) を取得。未登録なら initial_capital で初期化。

    Phase A Boyd cushion (2026-05-27): book別 equity peak を tracking、cushion計算 base。
    """
    row = con.execute(
        "SELECT hwm_value FROM book_hwm WHERE book_id = ?", [book]
    ).fetchone()
    if row is None:
        # 初回: initial_capital で seed
        con.execute(
            "INSERT INTO book_hwm (book_id, hwm_value, updated_at) VALUES (?, ?, CURRENT_TIMESTAMP)",
            [book, initial_capital],
        )
        return float(initial_capital)
    return float(row[0])


def update_book_hwm(con: duckdb.DuckDBPyConnection, book: str, current_equity: float) -> float:
    """HWM ratchet: current_equity > 現 HWM なら更新、戻り値は新 HWM。

    Phase A Boyd cushion: max equity を保持し続けることで drawdown 計算の正確な base に。
    """
    current_hwm = get_book_hwm(con, book, current_equity)
    if current_equity > current_hwm:
        con.execute(
            "UPDATE book_hwm SET hwm_value = ?, updated_at = CURRENT_TIMESTAMP WHERE book_id = ?",
            [current_equity, book],
        )
        return current_equity
    return current_hwm


def compute_boyd_cushion(
    equity: float, hwm: float, dd_hard_limit: float = -0.075, alpha: float = 1.0,
) -> float:
    """Boyd continuous drawdown cushion (frontier math).

    Stanford Boyd 論文 + arXiv 1710.01503 affine feedback law:
      cushion = max(0, 1 + dd / dd_hard_limit) ** alpha
      sizing_multiplier *= cushion で smooth に縮小

    本田くん哲学「決まった日数や%で固めない、動的にプラス/マイナス」を frontier 数学で実装。
    静的 2段階/N段階 discrete より optimal (連続関数で threshold-cliff effect なし)。

    Examples:
        cushion(equity=100, hwm=100) = 1.0 (満タン)
        cushion(equity=98, hwm=100) → dd=-2%、cushion ≈ 0.73
        cushion(equity=95, hwm=100) → dd=-5%、cushion ≈ 0.33
        cushion(equity=92.5, hwm=100) → dd=-7.5%、cushion = 0.0 (book pause)

    Args:
        equity: 現在 equity (initial + realized)
        hwm: book HWM (era 内 max equity)
        dd_hard_limit: book pause になる DD閾値 (default -0.075)
        alpha: smooth decay 指数 (1.0=linear、2.0=quadratic)

    Returns:
        cushion ∈ [0.0, 1.0]
    """
    if hwm <= 0:
        return 1.0  # safety: HWM未初期化なら full size
    dd = (equity - hwm) / hwm
    if dd >= 0:
        return 1.0  # HWM超え = full size
    if dd <= dd_hard_limit:
        return 0.0  # ハード上限到達 = pause
    # dd, dd_hard_limit ともに負数。dd/dd_hard_limit は正の比率 (0 → 1)。
    # cushion は dd=0 で 1.0、dd=dd_hard_limit で 0.0。逆比例で減算。
    return max(0.0, 1.0 - dd / dd_hard_limit) ** alpha


def book_account(
    con: duckdb.DuckDBPyConnection, book: str, initial_capital: float
) -> tuple[float, float, set[str]]:
    """ブックの資金状態を返す: (equity, free_cash, open_tickers)。

    equity     = 初期資金 + 実現損益（複利。pnlは手数料控除後）
    free_cash  = equity − 拘束中資金（open建玉の取得原価+entry手数料）
    open_tickers = そのブックで現在openの銘柄（重複建て防止+同時保有上限用）

    free_cash が新規建玉に使える上限。cron跨ぎでも over-deploy しない。
    """
    realized = con.execute(
        "SELECT COALESCE(SUM(pnl), 0) FROM positions WHERE book = ? AND status = 'closed'",
        [book],
    ).fetchone()[0]
    open_rows = con.execute(
        "SELECT ticker, entry_price, shares, COALESCE(commission, 0) "
        "FROM positions WHERE book = ? AND status = 'open'",
        [book],
    ).fetchall()
    committed = sum(ep * sh + cm for _, ep, sh, cm in open_rows)
    open_tickers = {r[0] for r in open_rows}
    equity = initial_capital + float(realized)
    free_cash = equity - committed
    return equity, free_cash, open_tickers


def get_connection(db_path: Path | str | None = None) -> duckdb.DuckDBPyConnection:
    """DuckDB接続を返す。db_path=Noneならデフォルトパス。"""
    path = Path(db_path) if db_path else DEFAULT_DB_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    con = connect_duckdb(path)
    _create_tables(con)
    return con


def _create_tables(con: duckdb.DuckDBPyConnection) -> None:
    """3テーブルを作成（存在しなければ）。"""

    con.execute("""
        CREATE SEQUENCE IF NOT EXISTS scans_id_seq START 1;
    """)
    con.execute("""
        CREATE TABLE IF NOT EXISTS scans (
            id INTEGER DEFAULT nextval('scans_id_seq') PRIMARY KEY,
            ticker TEXT NOT NULL,
            scanned_at TIMESTAMP NOT NULL,
            ma25_deviation REAL,
            nikkei_change REAL,
            p1 BOOLEAN,
            p2 BOOLEAN,
            p3 BOOLEAN,
            p4 BOOLEAN,
            p5 BOOLEAN,
            consensus INTEGER
        );
    """)

    con.execute("""
        CREATE SEQUENCE IF NOT EXISTS positions_id_seq START 1;
    """)
    con.execute("""
        CREATE TABLE IF NOT EXISTS positions (
            id INTEGER DEFAULT nextval('positions_id_seq') PRIMARY KEY,
            ticker TEXT NOT NULL,
            entry_price REAL NOT NULL,
            shares INTEGER NOT NULL,
            entry_date DATE NOT NULL,
            status TEXT NOT NULL CHECK (status IN ('open', 'closed')),
            exit_price REAL,
            exit_reason TEXT CHECK (
                exit_reason IS NULL
                OR exit_reason IN ('stop_loss', 'max_hold', 'take_profit', 'signal_reversal',
                                   'news_negative', 'gap_down_stop', 'trailing_stop')
            ),
            pnl REAL,
            closed_at TIMESTAMP,
            commission REAL DEFAULT 0.0
        );
    """)

    # 既存DBへの追加（冪等）
    con.execute("ALTER TABLE positions ADD COLUMN IF NOT EXISTS commission REAL DEFAULT 0.0;")
    # マルチブック化（2026-05-16）: 既存3ポジションは 'legacy' タグ
    con.execute("ALTER TABLE positions ADD COLUMN IF NOT EXISTS book TEXT DEFAULT 'legacy';")
    # 令和式 trailing stop (2026-05-25): max_price_seen を蓄積
    con.execute("ALTER TABLE positions ADD COLUMN IF NOT EXISTS max_price_seen REAL;")

    # Phase A Boyd cushion (2026-05-27): book別 HWM tracking
    # HWM = High-Water-Mark (各 book の era 内 max equity)
    # equity = initial + realized 監視で動的更新、cushion計算に使用
    con.execute("""
        CREATE TABLE IF NOT EXISTS book_hwm (
            book_id TEXT PRIMARY KEY,
            hwm_value REAL NOT NULL,
            updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
    """)

    con.execute("""
        CREATE TABLE IF NOT EXISTS monthly_pnl (
            year_month TEXT PRIMARY KEY,
            total_pnl REAL NOT NULL DEFAULT 0.0,
            drawdown_pct REAL NOT NULL DEFAULT 0.0,
            is_stopped BOOLEAN NOT NULL DEFAULT FALSE
        );
    """)

    # Phase 1 L-A: news_events (EDINET開示 + Qwen3分類結果の蓄積)
    con.execute("CREATE SEQUENCE IF NOT EXISTS news_events_id_seq START 1;")
    con.execute("""
        CREATE TABLE IF NOT EXISTS news_events (
            id INTEGER DEFAULT nextval('news_events_id_seq') PRIMARY KEY,
            scanned_at TIMESTAMP NOT NULL,
            ticker TEXT NOT NULL,
            doc_id TEXT NOT NULL,
            doc_type_code TEXT,
            filer_name TEXT,
            doc_description TEXT,
            impact TEXT,
            reason TEXT,
            body_len INTEGER,
            applied BOOLEAN DEFAULT FALSE,
            UNIQUE (ticker, doc_id)
        );
    """)
