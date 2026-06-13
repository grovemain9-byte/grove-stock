#!/usr/bin/env bash
# weekly_retrain.sh — 週次 PLT 再集計 + EVS 重み最適化 cron ラッパー
#
# 推奨 crontab 行 (正時を避けた土曜早朝 3:07 JST / 取引cron・HB cron と衝突しない):
#   7 3 * * 6  cd $HOME/grove-stock && bash scripts/weekly_retrain.sh >> logs/weekly_retrain_cron.log 2>&1
#
# 手動 dry-run:
#   bash scripts/weekly_retrain.sh --dry-run
#
# 動作:
#   1. 市場時間帯 (JST 9:00-11:30 / 12:30-15:30) は skip (exit 0)
#   2. scripts/weekly_retrain.py を実行
#   3. ログを logs/weekly_retrain_YYYYMMDD.log に出力
#   4. Python スクリプトが失敗したら exit 1 (cron がメール通知できるように)
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
GROVE_STOCK_DIR="$(dirname "$SCRIPT_DIR")"
PYTHON="$GROVE_STOCK_DIR/.venv/bin/python"
LOG_DIR="$GROVE_STOCK_DIR/logs"
DRY_RUN=0

for arg in "$@"; do
    case "$arg" in
        --dry-run) DRY_RUN=1 ;;
    esac
done

mkdir -p "$LOG_DIR"
LOG_FILE="$LOG_DIR/weekly_retrain_$(TZ=Asia/Tokyo date +%Y%m%d).log"

log() {
    echo "$(TZ=Asia/Tokyo date '+%Y-%m-%d %H:%M:%S') $*" | tee -a "$LOG_FILE"
}

# --- 市場時間チェック (JST) ---
JST_HOUR=$(TZ=Asia/Tokyo date +%H)
JST_MIN=$(TZ=Asia/Tokyo date +%M)
# 10# prefix で先頭ゼロによる octal 誤認を防ぐ (bash 算術の仕様)
JST_TIME=$((10#$JST_HOUR * 60 + 10#$JST_MIN))
MARKET_AM_START=$((9 * 60))       # 09:00
MARKET_AM_END=$((11 * 60 + 30))   # 11:30
MARKET_PM_START=$((12 * 60 + 30)) # 12:30
MARKET_PM_END=$((15 * 60 + 30))   # 15:30

if (( JST_TIME >= MARKET_AM_START && JST_TIME < MARKET_AM_END )) || \
   (( JST_TIME >= MARKET_PM_START && JST_TIME < MARKET_PM_END )); then
    log "SKIP: 市場時間帯 (JST ${JST_HOUR}:${JST_MIN}). weekly_retrain を実行しない"
    exit 0
fi

# --- 環境チェック ---
if [ ! -x "$PYTHON" ]; then
    log "ERROR: venv not found at $PYTHON"
    exit 1
fi

log "START: weekly_retrain (dry_run=$DRY_RUN)"
log "  GROVE_STOCK_DIR=$GROVE_STOCK_DIR"
log "  PYTHON=$PYTHON"

# --- 実行 ---
cd "$GROVE_STOCK_DIR"

EXTRA_ARGS=""
if [ "$DRY_RUN" -eq 1 ]; then
    EXTRA_ARGS="--skip-plt --skip-optimizer"
    log "DRY-RUN: --skip-plt --skip-optimizer で完走確認のみ"
fi

# shellcheck disable=SC2086
if "$PYTHON" scripts/weekly_retrain.py $EXTRA_ARGS >> "$LOG_FILE" 2>&1; then
    log "DONE: weekly_retrain 正常完了"
    exit 0
else
    log "FAILED: weekly_retrain が非 0 で終了 (詳細は $LOG_FILE)"
    exit 1
fi
