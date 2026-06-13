"""ユニバース管理 + セクター別MA25閾値自動算出。
2026-04-21: 13銘柄手動マップ → Prime全銘柄(1575) × ADV流動性フィルタ → ~1000銘柄
セクター閾値は S33(33業種) ごとの過去252日ボラティリティから k=2.0σ で自動算出。
"""
from __future__ import annotations

import logging
import os
import time
from datetime import datetime, timedelta
from pathlib import Path

import duckdb
import pandas as pd

from src.data.cache import CACHE_DB

logger = logging.getLogger("data.universe")

# S7: ADV取得の進捗チェックポイント（再開可能・時差バッチ）。
# code を PRIMARY KEY にし「取得済 = 行が存在」。adv_20d NULL = 取得したが
# データ無し（再取得不要）。J-Quants Light の429パンクを、バッチ分割＋
# バッチ間クールダウン＋途中保存で回避し、複数回に分けて完走させる。
_ADV_PROGRESS_DDL = """
CREATE TABLE IF NOT EXISTS adv_progress (
    code        VARCHAR PRIMARY KEY,
    adv_20d     DOUBLE,
    fetched_at  TIMESTAMP DEFAULT now()
)
"""


def _adv_db(db_path: str | Path | None, read_only: bool = False):
    con = duckdb.connect(str(db_path or CACHE_DB), read_only=read_only)
    if not read_only:
        con.execute(_ADV_PROGRESS_DDL)
    return con


def _is_rate_limit(err: Exception) -> bool:
    s = str(err).lower()
    return "429" in s or "too many" in s or "rate" in s


def _call_with_backoff(fn, *, what: str, retries: int = 5, base: float = 8.0):
    """J-Quants 429 用の指数バックオフ。429以外は即raise。

    Light プランはレート制限が厳しい。429を握り潰すと「全銘柄スキップ→
    ほぼ空universeをsave」という致命的 silent failure になるため、明示的に
    待ってリトライし、尽きたら raise（呼び出し側で停止判断）。
    """
    last: Exception | None = None
    for attempt in range(retries):
        try:
            return fn()
        except Exception as e:  # noqa: BLE001
            if not _is_rate_limit(e):
                raise
            last = e
            wait = base * (2 ** attempt)
            logger.warning("J-Quants 429 on %s (attempt %d/%d), sleeping %.0fs",
                           what, attempt + 1, retries, wait)
            time.sleep(wait)
    raise RuntimeError(f"J-Quants rate limit not cleared for {what}: {last}")

# 流動性フィルタ: 20日平均売買代金の下限（JPY）
ADV_MIN_JPY = float(os.getenv("ADV_MIN_JPY", "100000000"))  # 1億円
# ボラベース閾値の k (daily sigma × k)
SECTOR_THRESHOLD_K = float(os.getenv("SECTOR_THRESHOLD_K", "2.0"))
# フロア/シーリング (暴走防止)
THRESHOLD_FLOOR = -0.20  # -20%
THRESHOLD_CEILING = -0.03  # -3%


def _get_client():
    import jquantsapi
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).resolve().parent.parent.parent / ".env")
    key = (os.getenv("JQUANTS_API_KEY") or os.getenv("JQUANTS_REFRESH_TOKEN") or "").strip()
    if not key:
        raise RuntimeError("JQUANTS_REFRESH_TOKEN not set")
    return jquantsapi.ClientV2(api_key=key)


def fetch_universe(
    scale_categories: tuple = ("TOPIX Core30", "TOPIX Large70", "TOPIX Mid400",
                               "TOPIX Small 1", "TOPIX Small 2"),
    market: str = "Prime",
) -> pd.DataFrame:
    """対象ユニバース（Prime + 指定ScaleCat）を返す。
    Returns: df with Code, CoName, S33, S33Nm, ScaleCat columns.
    """
    c = _get_client()
    df = _call_with_backoff(c.get_list, what="get_list(equity master)")
    df = df[df["MktNmEn"] == market].copy()
    if scale_categories:
        df = df[df["ScaleCat"].isin(scale_categories)].copy()
    df["ticker"] = df["Code"].astype(str).str[:4]  # 5桁→4桁短縮
    return df[["ticker", "Code", "CoName", "S33", "S33Nm", "ScaleCat"]].reset_index(drop=True)


def compute_sector_thresholds(
    universe: pd.DataFrame,
    lookback_days: int = 252,
    k: float = SECTOR_THRESHOLD_K,
    sample_per_sector: int = 5,
) -> dict[str, float]:
    """S33業種別に過去lookback日の日次リターン標準偏差を算出し、
    閾値 = -k × sigma を返す。全銘柄取得は重いので業種ごと上位sample_per_sector銘柄で代表値を取る。

    Returns:
        {S33: threshold} — 例: {'医薬品': -0.045, '電気機器': -0.080}
    """
    c = _get_client()
    end = datetime.now()
    start = end - timedelta(days=int(lookback_days * 1.5) + 10)
    from_s, to_s = start.strftime("%Y%m%d"), end.strftime("%Y%m%d")

    result: dict[str, float] = {}
    for sector, grp in universe.groupby("S33Nm"):
        # ScaleCatでソートして上位銘柄を取る (Core30→Large70→Mid400順)
        scale_order = {"TOPIX Core30": 0, "TOPIX Large70": 1, "TOPIX Mid400": 2,
                       "TOPIX Small 1": 3, "TOPIX Small 2": 4}
        sorted_grp = grp.assign(_ord=grp["ScaleCat"].map(lambda x: scale_order.get(x, 9))).sort_values("_ord")
        sample = sorted_grp.head(sample_per_sector)

        sigmas = []
        for code in sample["Code"]:
            try:
                df = c.get_eq_bars_daily(code=code, from_yyyymmdd=from_s, to_yyyymmdd=to_s)
                if df is None or df.empty or "AdjC" not in df.columns:
                    continue
                close = df.sort_values("Date")["AdjC"].astype(float).dropna()
                if len(close) < 30:
                    continue
                ret = close.pct_change().dropna()
                sigmas.append(float(ret.std()))
            except Exception as e:
                logger.warning("vol calc failed %s: %s", code, e)
                continue

        if not sigmas:
            continue
        avg_sigma = sum(sigmas) / len(sigmas)
        threshold = -k * avg_sigma
        # フロア/シーリングでclip
        threshold = max(THRESHOLD_FLOOR, min(THRESHOLD_CEILING, threshold))
        result[sector] = threshold
        logger.info("sector=%s sigma=%.4f threshold=%.3f (n=%d)", sector, avg_sigma, threshold, len(sigmas))

    return result


def apply_adv_filter(
    universe: pd.DataFrame,
    adv_min_jpy: float = ADV_MIN_JPY,
    lookback_days: int = 20,
    max_tickers: int | None = None,
    throttle_sec: float = 0.5,
) -> pd.DataFrame:
    """過去lookback_days日の平均売買代金(Va)が adv_min_jpy 以上の銘柄だけ残す。
    max_tickers 指定時はADV降順で上位N件。

    J-Quants Light はレート制限が厳しい。throttle_sec で銘柄間に間隔を空け、
    429 は _call_with_backoff で待ってリトライ（握り潰さない＝空universe防止）。
    データ無し等の非429例外のみ skip（その銘柄を除外）。
    """
    c = _get_client()
    end = datetime.now()
    start = end - timedelta(days=int(lookback_days * 1.6) + 5)
    from_s, to_s = start.strftime("%Y%m%d"), end.strftime("%Y%m%d")

    advs = {}
    total = len(universe)
    for i, code in enumerate(universe["Code"]):
        try:
            df = _call_with_backoff(
                lambda: c.get_eq_bars_daily(
                    code=code, from_yyyymmdd=from_s, to_yyyymmdd=to_s),
                what=f"get_eq_bars_daily({code})")
            if df is None or df.empty or "Va" not in df.columns:
                continue
            va = df["Va"].astype(float).dropna().tail(lookback_days)
            if len(va) < lookback_days // 2:
                continue
            advs[code] = float(va.mean())
        except RuntimeError as e:
            # 429がリトライ尽きても解消しない＝続行不能。空universe保存を防ぐため停止
            raise RuntimeError(
                f"ADV filter aborted at {i}/{total} ({code}): {e}")
        except Exception:  # noqa: BLE001 — データ無し等は当該銘柄のみ除外
            continue
        if throttle_sec > 0:
            time.sleep(throttle_sec)
        if (i + 1) % 100 == 0:
            logger.info("ADV filter progress: %d/%d (kept %d)",
                        i + 1, total, len(advs))

    universe = universe.copy()
    universe["adv_20d"] = universe["Code"].map(advs)
    filtered = universe.dropna(subset=["adv_20d"])
    filtered = filtered[filtered["adv_20d"] >= adv_min_jpy].copy()
    filtered = filtered.sort_values("adv_20d", ascending=False).reset_index(drop=True)
    if max_tickers:
        filtered = filtered.head(max_tickers)
    return filtered


def reset_adv_progress(db_path: str | Path | None = None) -> None:
    """ADV進捗を全消去（壊れた部分結果からやり直す時）。"""
    con = _adv_db(db_path)
    con.execute("DELETE FROM adv_progress")
    con.close()
    logger.info("adv_progress reset")


def fetch_adv_progress(
    prime: pd.DataFrame,
    *,
    lookback_days: int = 20,
    batch_size: int = 150,
    cooldown_sec: float = 90.0,
    max_batches: int = 0,        # 0=制限なし。N=このrunでNバッチだけ→時差再開
    throttle_sec: float = 0.5,
    db_path: str | Path | None = None,
) -> dict:
    """Prime全銘柄のADVを**バッチ分割・バッチ間クールダウン・途中保存**で取得。

    1ルートで全1,800を投げると J-Quants Light が429パンク → batch_size 毎に
    区切り、バッチ間に cooldown_sec 休む。各銘柄は adv_progress に即保存する
    ので、429で尽きても・max_batches で打ち切っても**再実行で続きから**進む。

    Returns: {complete, done, total, remaining, batches_this_run}
    """
    all_codes = [str(c) for c in prime["Code"].tolist()]
    total = len(all_codes)

    con = _adv_db(db_path)
    done = {r[0] for r in con.execute("SELECT code FROM adv_progress").fetchall()}
    pending = [c for c in all_codes if c not in done]
    if not pending:
        con.close()
        return {"complete": True, "done": len(done), "total": total,
                "remaining": 0, "batches_this_run": 0}

    c = _get_client()
    end = datetime.now()
    start = end - timedelta(days=int(lookback_days * 1.6) + 5)
    from_s, to_s = start.strftime("%Y%m%d"), end.strftime("%Y%m%d")

    batches = 0
    i = 0
    while i < len(pending):
        if max_batches and batches >= max_batches:
            break
        batch = pending[i:i + batch_size]
        for code in batch:
            try:
                df = _call_with_backoff(
                    lambda: c.get_eq_bars_daily(
                        code=code, from_yyyymmdd=from_s, to_yyyymmdd=to_s),
                    what=f"get_eq_bars_daily({code})")
                adv = None
                if df is not None and not df.empty and "Va" in df.columns:
                    va = df["Va"].astype(float).dropna().tail(lookback_days)
                    if len(va) >= lookback_days // 2:
                        adv = float(va.mean())
            except RuntimeError as e:
                # 429がバックオフ尽きても解消せず＝続行不能。ここまでは保存済→
                # 再実行で続きから。空universe保存は build 側 MIN_SANE が防ぐ。
                con.close()
                raise RuntimeError(
                    f"ADV停止 {len(done)}/{total} ({code}): {e} "
                    f"／クールダウン後に再実行で継続")
            except Exception:  # noqa: BLE001 — 非429(データ無し等)は NULL 記録
                adv = None
            con.execute(
                "INSERT INTO adv_progress (code, adv_20d, fetched_at) "
                "VALUES (?, ?, ?)", [code, adv, datetime.now()])
            done.add(code)
            if throttle_sec > 0:
                time.sleep(throttle_sec)
        batches += 1
        i += batch_size
        remaining = total - len(done)
        logger.info("ADVバッチ%d完了: %d/%d (残%d)",
                    batches, len(done), total, remaining)
        more = i < len(pending) and not (max_batches and batches >= max_batches)
        if more and cooldown_sec > 0:
            logger.info("クールダウン %.0fs（429パンク防止）...", cooldown_sec)
            time.sleep(cooldown_sec)

    con.close()
    remaining = total - len(done)
    return {"complete": remaining == 0, "done": len(done), "total": total,
            "remaining": remaining, "batches_this_run": batches}


def build_filtered_from_progress(
    prime: pd.DataFrame, adv_min_jpy: float = ADV_MIN_JPY,
    db_path: str | Path | None = None,
) -> pd.DataFrame:
    """adv_progress（完了済）から ADV>=閾値 の universe を組み立てる。"""
    con = _adv_db(db_path, read_only=True)
    rows = con.execute(
        "SELECT code, adv_20d FROM adv_progress WHERE adv_20d IS NOT NULL"
    ).fetchall()
    con.close()
    advmap = {str(c): float(a) for c, a in rows}
    u = prime.copy()
    u["adv_20d"] = u["Code"].astype(str).map(advmap)
    f = u.dropna(subset=["adv_20d"])
    f = f[f["adv_20d"] >= adv_min_jpy].sort_values(
        "adv_20d", ascending=False).reset_index(drop=True)
    return f


def prime_codes_cached(
    codes: list[str], *, recent_within_days: int = 10,
    min_bars: int = 60, db_path: str | Path | None = None,
) -> set[str]:
    """与えた codes のうち daily_quotes に十分なバーが既にある集合。

    bulk_fetch の再開可否判定（既cached＝再取得不要）と完了判定に使う。
    done = 行数 >= min_bars かつ max(date) が recent_within_days 以内。
    """
    con = duckdb.connect(str(db_path or CACHE_DB), read_only=True)
    rows = con.execute(
        "SELECT code, COUNT(*) n, MAX(date) mx FROM daily_quotes "
        "WHERE code IN ({}) GROUP BY code".format(
            ",".join("?" * len(codes))), codes).fetchall() if codes else []
    con.close()
    cutoff = (datetime.now().date() - timedelta(days=recent_within_days))
    return {c for c, n, mx in rows if n >= min_bars and mx is not None and mx >= cutoff}


def compute_adv_from_cache(
    prime: pd.DataFrame, adv_min_jpy: float = ADV_MIN_JPY,
    lookback_days: int = 20, db_path: str | Path | None = None,
) -> pd.DataFrame:
    """キャッシュ済 daily_quotes.value(売買代金) から 20日ADV を算出しフィルタ。

    **API呼出ゼロ**（bulk_fetch で取得済の bars を再利用＝二重取得排除）。
    1クエリの window で全コード集計（F2: 銘柄毎接続しない）。
    """
    codes = [str(c) for c in prime["Code"].tolist()]
    if not codes:
        return prime.iloc[0:0].assign(adv_20d=pd.Series(dtype=float))
    con = duckdb.connect(str(db_path or CACHE_DB), read_only=True)
    rows = con.execute(
        """
        SELECT code, AVG(value) AS adv, COUNT(*) AS n FROM (
          SELECT code, value,
                 ROW_NUMBER() OVER (PARTITION BY code ORDER BY date DESC) rn
          FROM daily_quotes
          WHERE value IS NOT NULL AND code IN ({})
        ) WHERE rn <= ? GROUP BY code
        """.format(",".join("?" * len(codes))),
        codes + [lookback_days],
    ).fetchall()
    con.close()
    advmap = {str(c): float(a) for c, a, n in rows if n >= lookback_days // 2}
    u = prime.copy()
    u["adv_20d"] = u["Code"].astype(str).map(advmap)
    f = u.dropna(subset=["adv_20d"])
    f = f[f["adv_20d"] >= adv_min_jpy].sort_values(
        "adv_20d", ascending=False).reset_index(drop=True)
    return f


def get_threshold_by_sector(ticker: str, universe: pd.DataFrame,
                            thresholds: dict[str, float],
                            default: float = -0.07) -> float:
    """銘柄コードからS33業種名を引き、閾値を返す。"""
    row = universe[universe["ticker"] == ticker]
    if row.empty:
        return default
    sector = row.iloc[0]["S33Nm"]
    return thresholds.get(sector, default)
