# grove-stock Operations Manual

> 火曜稼働以降、毎朝確認すべき観測 SQL とアラート閾値。
> Phase 0+ EVS/PLT (2026-05-21 Grove刷新) 対応版。

---

## 🌅 毎朝の Health Check (5分)

### 1. cron 実行確認
```bash
crontab -l | grep grove
tail -50 logs/scan_pipeline.log
tail -50 logs/hb_learning.log
```

期待: 最新エントリが本日朝 9:30 以降、ERROR/Exception なし。

### 2. 今日の decision_shadow 記録数
```sql
SELECT COUNT(*) AS rows, COUNT(DISTINCT book) AS books
FROM decision_shadow
WHERE decided_date = CURRENT_DATE;
```
期待: rows ≥ 5 × 100 (5 book × 100候補相当)、books = 5。
**異常**: rows=0 → cron 未起動 / books<5 → 一部 book skip。

### 3. sizing_source 分布（v2 router が動いているか）
```sql
SELECT sizing_source, COUNT(*) AS n,
       ROUND(AVG(cap_pct_actual) * 100, 1) AS avg_cap_pct
FROM decision_shadow
WHERE decided_date = CURRENT_DATE AND decision = 'go'
GROUP BY sizing_source
ORDER BY n DESC;
```
期待初週: `evs_fallback` が 8 割以上（PLT cell 未到達多数）+ `plt_cold` 少々。
**1ヶ月後の理想**: `plt_warm`/`plt_hot` が増え、`evs_fallback` が減る。
**異常**: 全部 `evs_fallback` → router 経路バグの可能性。

### 4. exploration_flag 発火率（ε-greedy が動いているか）
```sql
SELECT
    SUM(CASE WHEN exploration_flag THEN 1 ELSE 0 END) AS n_explore,
    COUNT(*) AS n_total,
    ROUND(100.0 * SUM(CASE WHEN exploration_flag THEN 1 ELSE 0 END) / NULLIF(COUNT(*), 0), 1) AS rate_pct
FROM decision_shadow
WHERE decided_date = CURRENT_DATE AND sizing_source IS NOT NULL;
```
期待: rate_pct ≈ 20% (n<50時) → ε=20% schedule の発現。
**異常**: rate_pct=0 → exploration コードパス通ってない。

### 5. PLT cell 成長（成績表が育っているか）
```sql
SELECT confidence, COUNT(*) AS n_cells, SUM(n_samples) AS total_samples
FROM plt_cells GROUP BY confidence ORDER BY confidence;
```
期待: 火曜 → cold:5 warm:1 hot:0
1週間後: cold:15 warm:8 hot:2
1ヶ月後: cold:30 warm:30 hot:15

---

## 🔬 週次レポート (金曜夕方)

### 6. exploration vs exploitation EV 比較
```bash
PYTHONPATH=. .venv/bin/python scripts/analyze_exploration_ev.py --days 7
```
- `exploration` の avg_pnl_pct と `exploitation` の比較
- exploration > exploitation → ε 維持
- exploration < exploitation で有意 (n≥50) → ε 下げ検討

### 7. EVS factor 相関（要素ごとの寄与）
```sql
WITH r AS (
    SELECT cap_pct_actual, cf_pnl_pct, evs_total,
           CAST(json_extract(evs_components_json, '$.f1_signal_strength') AS DOUBLE) AS f1,
           CAST(json_extract(evs_components_json, '$.f2_deviation_depth') AS DOUBLE) AS f2,
           CAST(json_extract(evs_components_json, '$.f3_rsi_oversold') AS DOUBLE) AS f3,
           CAST(json_extract(evs_components_json, '$.f4_bb_penetration') AS DOUBLE) AS f4,
           CAST(json_extract(evs_components_json, '$.f5_volume_decline') AS DOUBLE) AS f5
    FROM decision_shadow
    WHERE cf_pnl_pct IS NOT NULL
      AND decided_date >= CURRENT_DATE - INTERVAL '30' DAY
)
SELECT
    COUNT(*) AS n,
    ROUND(CORR(f1, cf_pnl_pct), 4) AS f1_corr,
    ROUND(CORR(f2, cf_pnl_pct), 4) AS f2_corr,
    ROUND(CORR(f3, cf_pnl_pct), 4) AS f3_corr,
    ROUND(CORR(f4, cf_pnl_pct), 4) AS f4_corr,
    ROUND(CORR(f5, cf_pnl_pct), 4) AS f5_corr
FROM r;
```
**事前仮説 (Phase 0+ bootstrap発見)**: F2 (deviation_depth) は負の相関の可能性。
ここで F2_corr < -0.10 (n≥30) なら scipy 重み最適化で F2 重みが下がる。

### 8. Weekly retrain 実行
```bash
PYTHONPATH=. .venv/bin/python scripts/weekly_retrain.py
```
出力: `[optimizer] improvement: +X.XX` が正なら新重みを採用、負ならスキップ。

---

## 🚨 アラート閾値

| 観測 | 警告 | 緊急 |
|------|------|------|
| 日次 decision_shadow rows | < 100 | < 10 |
| ε発火率 | < 5% | = 0% |
| sizing_source = evs_fallback 比率 (1ヶ月後) | > 90% | > 99% |
| PLT cells confidence=cold 比率 (1ヶ月後) | > 80% | > 95% |
| cf backfill 未確定率 (10営業日後) | > 50% | > 90% |
| go決定の平均 cf_pnl_pct (n≥30) | < -1.5% | < -3% |

---

## 🛠 トラブルシューティング

### "全部 evs_fallback" 問題
→ PLT cell が空である or features_to_cell の出力が不一致。確認:
```sql
SELECT COUNT(*) FROM plt_cells;  -- 0 なら bootstrap_plt.py 走らせる
SELECT DISTINCT cell_id FROM decision_shadow WHERE decided_date = CURRENT_DATE;
```

### "exploration 発火しない" 問題
→ exploration_rate(global_n) が想定値か:
```sql
SELECT SUM(n_samples) AS global_n FROM plt_cells;
-- global_n < 50 → eps=20% / >=50 → eps=10%
```

### "PLT cell が成長しない" 問題
→ bootstrap_plt.py は positions(closed) ベース。 closed が出てくる速度がボトルネック。
平均 hold 1.7 日 (前 session 計測値) なので、entry 後 1-2 営業日で closed 増える想定。

### "cf_pnl_pct が NULL のまま" 問題
→ backfill_counterfactuals は monitor exit が実バーで成立した時点で確定する設計。
hold 5 営業日上限なので、最大 5 日待ち。 5 日経っても NULL なら simulate ロジックバグ疑い。

---

## 📅 重要マイルストーン

| 日付 | 期待状態 | 行動 |
|------|---------|------|
| 5/22 (火) 9:30 | 初回 cron 起動 | health check (1-5) |
| 5/23 (水) 9:30 | 2日目稼働 + cf 確定 1-3 件 | sizing_source 分布変化観測 |
| 5/26 (月) | n+25 trades 目標 | exploration EV 中間集計 |
| 5/30 (金) | n+50 trades 目標 | weekly_retrain 初回実行 |
| 6/20 (金) | n+200 trades 目標 | PLT cell hot 数を Grove に報告 |

---

## 📝 仕様の出典

- EVS: `src/sizing/evs.py` (10 factors, Grove 2026-05-21 重み)
- PLT: `src/sizing/plt.py` (6軸×432セル)
- Router: `src/sizing/router.py` (ε-greedy schedule)
- Optimizer: `src/sizing/optimizer.py` (scipy SLSQP)
- 設計議事録: `docs/grove-stock/build_diary.md` 2026-05-21 entry
