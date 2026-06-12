#!/usr/bin/env bash
# grove-stock cron 設定 v2 (mini側)
# 2026-04-21: 13銘柄30分毎 → 494銘柄 1日3回 + daily_update
# - daily_update: 平日 15:05 (大引け直後、cache更新)
# - scan (entry判定): 平日 15:10 (翌日寄付向けシグナル確定)
# - monitor (exit判定): 平日 9:00 / 12:30 / 15:00
set -euo pipefail

GROVE_STOCK_DIR="$HOME/grove-stock"
PYTHON="$GROVE_STOCK_DIR/.venv/bin/python"
LOG_DIR="$GROVE_STOCK_DIR/logs"

[ -x "$PYTHON" ] || { echo "ERROR: venv not set up"; exit 1; }
mkdir -p "$LOG_DIR"

TMP=$(mktemp)
crontab -l 2>/dev/null | grep -v "grove-stock" > "$TMP" || true

cat >> "$TMP" <<EOF

# === grove-stock BNF Swing v2 (mini, 494銘柄) — $(date +%F) ===
# daily_update: cache更新 (平日15:05)
5 15 * * 1-5 cd $GROVE_STOCK_DIR && PYTHONPATH=$GROVE_STOCK_DIR $PYTHON -m src.data.daily_update >> $LOG_DIR/daily_update.log 2>&1
# scan (entry候補確定): 平日15:10
10 15 * * 1-5 cd $GROVE_STOCK_DIR && PYTHONPATH=$GROVE_STOCK_DIR $PYTHON src/main.py --dry-run >> $LOG_DIR/scan.log 2>&1
# monitor (exit判定): 平日 9:00 / 12:30 / 15:00
0 9 * * 1-5 cd $GROVE_STOCK_DIR && PYTHONPATH=$GROVE_STOCK_DIR $PYTHON src/main.py --dry-run >> $LOG_DIR/scan.log 2>&1
30 12 * * 1-5 cd $GROVE_STOCK_DIR && PYTHONPATH=$GROVE_STOCK_DIR $PYTHON src/main.py --dry-run >> $LOG_DIR/scan.log 2>&1
0 15 * * 1-5 cd $GROVE_STOCK_DIR && PYTHONPATH=$GROVE_STOCK_DIR $PYTHON src/main.py --dry-run >> $LOG_DIR/scan.log 2>&1
EOF

crontab "$TMP"
rm -f "$TMP"
echo "=== mini cron v2 installed ==="
crontab -l | grep -E "grove-stock|daily_update|^# ==="
