# Kill Criterion — book別 max_concurrent + consensus DESC ソート (commit c9254b4)

> **目的**: forward paper 観察で「希望的観測でNo-Goを延ばす」競輪PJ崩壊パターンを再発させない。判定基準を事前登録し、機械的に判定する。
>
> **作成**: 2026-05-24
> **対象commit**: `c9254b4` (book別 max_concurrent + consensus DESC sort)
> **観察期間**: 2026-05-25 〜 2026-06-08 (10営業日)
> **判定日**: 2026-06-08 終値後
> **判定責任**: 統括Davis (Groveが「キル判断委任」と明示した場合のみ自律実行可)

---

## GO継続条件 (全て満たす場合のみ修正維持)

| # | 条件 | 検証SQL |
|---|------|--------|
| G1 | consensus=5 の go率 ≥ 70% | (下記Q1) |
| G2 | p50m 同時保有数 ≥ 10銘柄の日が3日以上 | (下記Q2) |
| G3 | 全book合計 net PnL > -¥200,000 (許容drawdown) | (下記Q3) |
| G4 | max_concurrent_full の pass率 < 30% (旧82%から改善) | (下記Q4) |

---

## KILL条件 (1つでも該当でロールバック)

| # | 条件 | 検証SQL | アクション |
|---|------|--------|---------|
| K1 | max_concurrent_full が再び全decisionの50%超 | (Q4) | 修正Bを巻き戻し / max_concurrent値を再調整 |
| K2 | p50m 勝率が30%以下に転落 (n≥15で評価) | (Q5) | p50m の sizing 層を疑う / forward継続して原因特定 |
| K3 | paper cron が3日連続停止 | `ls -lh logs/scan.log` の mtime | cron復旧 / 観察期間を仕切り直し |
| K4 | エントリー件数/日 が0または100超 | (Q6) | over/under エントリーの原因trace |
| K5 | consensus=5 の go率が依然 < 30% | (Q1) | consensus DESC sort が機能してない → コードバグ疑い |

---

## 監視SQL集

### Q1: consensus別 go率 (5/25以降)
```sql
SELECT consensus,
  COUNT(*) AS n,
  SUM(CASE WHEN decision='go' THEN 1 ELSE 0 END) AS go_n,
  ROUND(SUM(CASE WHEN decision='go' THEN 1 ELSE 0 END)*100.0/COUNT(*),1) AS go_pct
FROM decision_shadow
WHERE decided_date >= DATE '2026-05-25'
GROUP BY consensus ORDER BY consensus DESC;
```

### Q2: p50m 日別 同時保有数
```sql
SELECT entry_date, COUNT(*) FILTER (WHERE status='open' OR closed_at > entry_date) AS held
FROM positions
WHERE book='p50m' AND entry_date >= DATE '2026-05-25'
GROUP BY entry_date ORDER BY entry_date;
```

### Q3: 全book net PnL (5/25以降の新規entry のみ)
```sql
SELECT
  ROUND(SUM(pnl),0) AS net_pnl,
  COUNT(*) AS n_closed
FROM positions
WHERE status='closed' AND entry_date >= DATE '2026-05-25';
```

### Q4: pass理由分布
```sql
SELECT council_reason, COUNT(*),
  ROUND(COUNT(*)*100.0/SUM(COUNT(*)) OVER (), 1) AS pct
FROM decision_shadow
WHERE decision='pass' AND decided_date >= DATE '2026-05-25'
GROUP BY council_reason ORDER BY COUNT(*) DESC;
```

### Q5: p50m 勝率推移
```sql
SELECT COUNT(*) AS n,
  ROUND(SUM(CASE WHEN pnl > 0 THEN 1 ELSE 0 END)*100.0/COUNT(*),1) AS win_pct,
  ROUND(SUM(pnl),0) AS net_pnl
FROM positions
WHERE book='p50m' AND status='closed' AND entry_date >= DATE '2026-05-25';
```

### Q6: 日別エントリー件数
```sql
SELECT entry_date, COUNT(*) AS n_entered
FROM positions
WHERE entry_date >= DATE '2026-05-25'
GROUP BY entry_date ORDER BY entry_date;
```

---

## 中間チェックポイント

| 日付 | やること |
|------|---------|
| 2026-05-25 (Mon) 9:30 | 初回scan後にQ1/Q4実行。consensus=5でgoが1件以上出てるか確認 (出てなければK5緊急発火) |
| 2026-05-25 (Mon) 15:30 | 1日終了後にQ6で entry件数確認 (旧~5件 vs 新数十件のオーダー差) |
| 2026-05-30 (Fri) | 5営業日中間レビュー。Q1-Q6全実行。中間トレンドを建築日誌に記録 |
| 2026-06-08 (Mon) | 最終判定。GO/KILL条件で機械的判定 → 建築日誌に判定結果と次アクション記録 |

---

## 実行スクリプト

```bash
bash ~/grove-stock/scripts/verify_max_concurrent_fix.sh
```

このスクリプトがQ1-Q6を順次実行し、GO/KILL条件と照合した結果を出力。

---

## 判定結果ログ (記入欄)

### 2026-05-25 初回確認
- [ ] Q1実行: consensus=5 go率 = ___ %
- [ ] Q6実行: 当日entry件数 = ___ 件
- [ ] 異常: なし / あり (詳細: )

### 2026-05-30 中間レビュー
- [ ] Q1-Q6全実行
- [ ] 中間判定: 継続 / 早期KILL
- [ ] 詳細:

### 2026-06-08 最終判定
- [ ] GO条件4つ判定: G1=__ G2=__ G3=__ G4=__
- [ ] KILL条件5つ判定: K1=__ K2=__ K3=__ K4=__ K5=__
- [ ] **最終判定: GO継続 / KILL→ロールバック / 一部修正→再観察**
- [ ] 次アクション:
