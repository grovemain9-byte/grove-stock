"""継続ループ MVP（Shadowモード）: 仮説→複数レジームwalk-forward門→記録。

Groveビジョン「常にデータ取り分析し動的最適で稼ぐAIエージェントチーム」の
*規律ループ*の決定論骨格。LLM agent層(Strategist の創造的仮説生成)は後。今は:
- Researcher = 各仮説を cost込み walk-forward 計測（3年次窓 vs G3 frozen基準）
- Guardian/Judge = アーキ過学習門を機械適用し verdict 記録
- **Shadow = strategy_params に一切書かない**（記録専用。起動≠適用の分離）

過学習門（不可侵・コード化）:
- 1変更のみ（evolution-rules）= 各仮説は G3 から単一paramだけ差分
- 3年次窓すべてで Sharpe↑ かつ maxDD非悪化 のみ GO（1-2窓=レジーム依存→PASS）
- per-stock最適化は登録しない（式/グローバルparamのみ）

decision_shadow と同様、記録は履歴（評価毎に追記、同日は置換で冪等）。
Brier的中率は本ループを実データで反復運用して初めて蓄積（今は verdict 記録）。
"""
from __future__ import annotations

import json
import logging
from datetime import date, datetime

import duckdb

from src.backtest.runner import compute_sector_thresholds_from_cache
from src.backtest.engine import run_backtest
from src.data.db import DEFAULT_DB_PATH

logger = logging.getLogger("measurement.hypothesis_loop")

# 3年次窓（レジーム多様性。down相場 Y2 を必ず含む＝踏み絵）
WINDOWS = [
    ("2023-05-01", "2024-04-30", "Y1"),
    ("2024-05-01", "2025-04-30", "Y2"),
    ("2025-05-01", "2026-05-15", "Y3"),
]
# G3 凍結基準 param（cost込で評価）
# 2026-05-20: 本田MTG bridge 用に max_hold_days/take_profit_dev/per_stock_uptrend_required 追加。
# 既存DEFAULT(=15, =0.0, =False)と一致 → G3 baseline 数値は不変。
G3 = dict(consensus_min=3, stop_loss=-0.07, p4_required=False, p4_threshold=0.0,
          max_hold_days=15, take_profit_dev=0.0, per_stock_uptrend_required=False)

# 仮説レジストリ: 各々 G3 からの**単一**param差分（1変更ルール=Guardian強制）
HYPOTHESES: dict[str, dict] = {
    "regime_filter": {"p4_required": True},   # 弱気時のみ逆張り（wf検証で3/3だった本命）
    "stop_tight_5": {"stop_loss": -0.05},
    "consensus_4": {"consensus_min": 4},
    # 2026-05-20: 本田MTG bridge — 単一axis検証
    "extended_hold": {"max_hold_days": 25},                  # 「持つ時間長く」
    "asymmetric_tp": {"take_profit_dev": 0.02},              # 「1%入→2%で逃げ」
    "ema900_uptrend": {"per_stock_uptrend_required": True},  # 「EMA900角度右肩上がり」(Y3のみ評価可)
}

_DDL = """
CREATE SEQUENCE IF NOT EXISTS hypothesis_shadow_id_seq START 1;
CREATE TABLE IF NOT EXISTS hypothesis_shadow (
    id INTEGER DEFAULT nextval('hypothesis_shadow_id_seq') PRIMARY KEY,
    eval_date     DATE NOT NULL,
    name          VARCHAR NOT NULL,
    param_delta   TEXT NOT NULL,
    windows_json  TEXT NOT NULL,
    windows_passed INTEGER NOT NULL,
    verdict       VARCHAR NOT NULL,
    applied       BOOLEAN NOT NULL DEFAULT FALSE,  -- shadow: 常にFALSE
    recorded_at   TIMESTAMP DEFAULT now()
)
"""

_MAXDD_EPS = 0.005  # maxDD「非悪化」許容（0.5%以内の悪化は同等とみなす）


def _metrics(th, start, end, params):
    try:
        m = run_backtest(
            sector_thresholds=th, max_concurrent_positions=7, p4_threshold=0.0,
            start_date=start, end_date=end, slippage_bps=10.0,
            apply_commission=True,
            consensus_min=params["consensus_min"], stop_loss=params["stop_loss"],
            p4_required=params["p4_required"],
            max_hold_days=params["max_hold_days"],
            take_profit_dev=params["take_profit_dev"],
            per_stock_uptrend_required=params["per_stock_uptrend_required"],
        ).metrics()
    except RuntimeError as e:
        # 警戒期不足は honest sentinel として返す（他の RuntimeError は再raise）
        if str(e).startswith("insufficient_warmup"):
            return {"insufficient_warmup": True, "closed": 0, "msg": str(e)}
        raise
    if m.get("closed", 0) == 0:
        return None
    return {"closed": m["closed"], "sharpe": m.get("sharpe_per_trade", 0.0),
            "maxdd": m["max_drawdown"], "avg": m["avg_return"],
            "win": m["win_rate"]}


def evaluate(th, name: str, delta: dict, _base_cache: dict) -> dict:
    """1仮説を3年次窓で G3 と比較。Guardian: 単一param差分のみ許可。"""
    if len(delta) != 1:
        raise ValueError(f"1変更ルール違反 {name}: {delta}")  # Guardian不可侵
    cand = {**G3, **delta}
    wins = []
    for s, e, tag in WINDOWS:
        if tag not in _base_cache:
            _base_cache[tag] = _metrics(th, s, e, G3)
        b = _base_cache[tag]
        c = _metrics(th, s, e, cand)
        if isinstance(c, dict) and c.get("insufficient_warmup"):
            wins.append({"w": tag, "pass": False, "note": "insufficient_warmup"})
            continue
        if b is None or c is None:
            wins.append({"w": tag, "pass": False, "note": "no_data"})
            continue
        passed = (c["sharpe"] > b["sharpe"]
                  and c["maxdd"] >= b["maxdd"] - _MAXDD_EPS)
        wins.append({"w": tag, "pass": bool(passed),
                     "base": b, "cand": c})
    npass = sum(1 for w in wins if w.get("pass"))
    verdict = "GO" if npass == len(WINDOWS) else "PASS"  # 3/3のみ構造的=GO
    return {"name": name, "delta": delta, "windows": wins,
            "windows_passed": npass, "verdict": verdict}


def run_loop(db_path: str | None = None, on: date | None = None) -> list[dict]:
    """全仮説を評価し hypothesis_shadow に記録（適用は一切しない=Shadow）。"""
    eval_date = on or date.today()
    th = compute_sector_thresholds_from_cache(k=2.0)
    base_cache: dict = {}
    results = []
    con = duckdb.connect(str(db_path) if db_path else str(DEFAULT_DB_PATH))
    try:
        for stmt in _DDL.strip().split(";"):
            if stmt.strip():
                con.execute(stmt)
        con.execute("DELETE FROM hypothesis_shadow WHERE eval_date = ?",
                    [eval_date])  # 同日冪等
        for name, delta in HYPOTHESES.items():
            r = evaluate(th, name, delta, base_cache)
            con.execute(
                "INSERT INTO hypothesis_shadow "
                "(eval_date,name,param_delta,windows_json,windows_passed,"
                "verdict,applied) VALUES (?,?,?,?,?,?,FALSE)",
                [eval_date, name, json.dumps(delta),
                 json.dumps(r["windows"]), r["windows_passed"], r["verdict"]],
            )
            results.append(r)
            logger.info("hypothesis %s: %d/%d windows → %s (shadow:未適用)",
                        name, r["windows_passed"], len(WINDOWS), r["verdict"])
    finally:
        con.close()
    return results


def main():
    logging.disable(logging.WARNING)
    res = run_loop()
    print("=== 継続ループ Shadow評価（G3 vs 単一param仮説・cost込・適用せず）===")
    for r in res:
        print(f"\n[{r['name']}] Δ={r['delta']} → {r['verdict']} "
              f"({r['windows_passed']}/{len(WINDOWS)}窓)")
        for w in r["windows"]:
            if "base" not in w:
                print(f"  {w['w']}: {w.get('note','-')}")
                continue
            b, c = w["base"], w["cand"]
            mk = "★" if w["pass"] else " "
            print(f"  {w['w']}{mk} G3 Sh{b['sharpe']:+.3f}/DD{b['maxdd']*100:5.1f}%"
                  f" → 候補 Sh{c['sharpe']:+.3f}/DD{c['maxdd']*100:5.1f}%"
                  f" (決着{c['closed']} 勝率{c['win']*100:.0f}%)")
    go = [r["name"] for r in res if r["verdict"] == "GO"]
    print(f"\n構造的(3/3)=GO候補: {go or 'なし'} "
          f"※Shadow記録のみ・strategy_params未変更（起動≠適用）")
    print("Brierは本ループを週次反復運用して蓄積→advisory→autonomous へ権限漸増")


if __name__ == "__main__":
    main()
