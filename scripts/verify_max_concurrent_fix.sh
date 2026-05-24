#!/usr/bin/env bash
# verify_max_concurrent_fix.sh
# commit c9254b4 (book別 max_concurrent + consensus DESC) の forward paper 効果検証
# 使い方: bash scripts/verify_max_concurrent_fix.sh
# kill_criterion.md のQ1-Q6を順次実行し、GO/KILL条件を機械判定する。

set -e
cd "$(dirname "$0")/.."

DB="data/grove_stock.duckdb"
SINCE="DATE '2026-05-25'"
PY=".venv/bin/python"

echo "=========================================="
echo "  forward paper verification (commit c9254b4)"
echo "  since: $SINCE"
echo "=========================================="
echo

$PY -c "
import duckdb
con = duckdb.connect('$DB', read_only=True)

def q(label, sql):
    print(f'--- {label} ---')
    try:
        for row in con.execute(sql).fetchall():
            print(row)
    except Exception as e:
        print(f'ERROR: {e}')
    print()

# Q1: consensus別 go率
q('Q1 consensus別 go率', '''
SELECT consensus, COUNT(*) AS n,
  SUM(CASE WHEN decision=\\'go\\' THEN 1 ELSE 0 END) AS go_n,
  ROUND(SUM(CASE WHEN decision=\\'go\\' THEN 1 ELSE 0 END)*100.0/COUNT(*),1) AS go_pct
FROM decision_shadow
WHERE decided_date >= $SINCE
GROUP BY consensus ORDER BY consensus DESC
''')

# Q2: p50m 日別 同時保有
q('Q2 p50m 日別エントリー数', '''
SELECT entry_date, COUNT(*) AS entered
FROM positions
WHERE book=\\'p50m\\' AND entry_date >= $SINCE
GROUP BY entry_date ORDER BY entry_date
''')

# Q3: 全book net PnL (5/25以降)
q('Q3 全book net PnL (5/25以降entry)', '''
SELECT COUNT(*) AS n_closed, ROUND(SUM(pnl),0) AS net_pnl
FROM positions
WHERE status=\\'closed\\' AND entry_date >= $SINCE
''')

# Q4: pass理由分布
q('Q4 pass理由分布', '''
SELECT council_reason, COUNT(*) AS n,
  ROUND(COUNT(*)*100.0/SUM(COUNT(*)) OVER (), 1) AS pct
FROM decision_shadow
WHERE decision=\\'pass\\' AND decided_date >= $SINCE
GROUP BY council_reason ORDER BY COUNT(*) DESC
''')

# Q5: p50m 勝率
q('Q5 p50m 勝率 (5/25以降)', '''
SELECT COUNT(*) AS n,
  ROUND(SUM(CASE WHEN pnl > 0 THEN 1 ELSE 0 END)*100.0/NULLIF(COUNT(*),0),1) AS win_pct,
  ROUND(SUM(pnl),0) AS net_pnl
FROM positions
WHERE book=\\'p50m\\' AND status=\\'closed\\' AND entry_date >= $SINCE
''')

# Q6: 日別 entry件数
q('Q6 日別 entry件数 (全book)', '''
SELECT entry_date, COUNT(*) AS n
FROM positions
WHERE entry_date >= $SINCE
GROUP BY entry_date ORDER BY entry_date
''')

# GO/KILL 簡易判定
print('=== GO/KILL 簡易判定 ===')
c5 = con.execute(f'''
SELECT ROUND(SUM(CASE WHEN decision=\\'go\\' THEN 1 ELSE 0 END)*100.0/NULLIF(COUNT(*),0),1)
FROM decision_shadow WHERE consensus=5 AND decided_date >= $SINCE
''').fetchone()[0]
print(f'G1: consensus=5 go率 = {c5}% (>=70% でGO継続)')

mcf = con.execute(f'''
SELECT ROUND(SUM(CASE WHEN council_reason=\\'max_concurrent_full\\' THEN 1 ELSE 0 END)*100.0/NULLIF(COUNT(*),0),1)
FROM decision_shadow WHERE decision=\\'pass\\' AND decided_date >= $SINCE
''').fetchone()[0]
print(f'G4/K1: max_concurrent_full pass率 = {mcf}% (<30% でGO継続 / >50% でKILL)')

net = con.execute(f'''
SELECT ROUND(SUM(pnl),0) FROM positions
WHERE status=\\'closed\\' AND entry_date >= $SINCE
''').fetchone()[0]
print(f'G3: 全book net PnL = ¥{net} (>-200,000 でGO継続)')
"
