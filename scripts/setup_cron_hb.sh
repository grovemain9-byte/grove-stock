#!/usr/bin/env bash
# grove-stock HB（自律学習）cron — "自律的に育つ会社"の脈。
# 取引cron(setup_cron_mini.sh)とは独立・追加（"grove-hb"タグで衝突回避）。
#
# すべて shadow/記録/read-only: strategy_params も実資金も一切変更しない。
# 昇格/適用/実弾化は Grove ゲート（HB自律ではやらない＝KAIROS不可侵）。
#
#   日次 15:45 平日 : capital_tracker snapshot + decision_shadow backfill
#                     （資金回転把握・go/pass反実仮想を出口日基準で確定）
#   週次 土 08:00   : hypothesis_loop(Shadow) + A/B集計 + Brier素材蓄積
#                     （仮説を複数レジームwf門で評価し記録・未適用）
#
# 個人=Brier / チーム=A/B track / 部署=業種集計 / 会社=総equity の
# 成長メトリクスが人手ゼロで毎週自動蓄積＝組織が"自律的に育つ"土壌が回る。
set -euo pipefail

GROVE_STOCK_DIR="$HOME/grove-stock"
PYTHON="$GROVE_STOCK_DIR/.venv/bin/python"
LOG_DIR="$GROVE_STOCK_DIR/logs"

[ -x "$PYTHON" ] || { echo "ERROR: venv not set up"; exit 1; }
mkdir -p "$LOG_DIR"

TMP=$(mktemp)
# 既存HB行を全除去して重複防止（取引cron=main.py/daily_update は温存）。
# 注意: コマンド行は "grove-hb" を含まない＝コメント+コマンド両方に共通する
# "hb_learning.py" も対象にしないと再実行で重複累積する（実バグ修正 2026-05-17）。
crontab -l 2>/dev/null | grep -vE "grove-hb|hb_learning\.py" > "$TMP" || true

cat >> "$TMP" <<EOF

# === grove-hb 自律学習（shadow・記録のみ・適用ゼロ） — $(date +%F) ===
# 日次 15:45 平日: 資金回転snapshot + 反実仮想backfill   # grove-hb
45 15 * * 1-5 cd $GROVE_STOCK_DIR && PYTHONPATH=$GROVE_STOCK_DIR $PYTHON scripts/hb_learning.py --daily >> $LOG_DIR/hb_learning.log 2>&1
# 週次 土08:00: 仮説ループ(Shadow) + A/B集計                # grove-hb
0 8 * * 6 cd $GROVE_STOCK_DIR && PYTHONPATH=$GROVE_STOCK_DIR $PYTHON scripts/hb_learning.py --weekly >> $LOG_DIR/hb_learning.log 2>&1
EOF

crontab "$TMP"
rm -f "$TMP"
echo "=== grove-hb cron installed（取引cronは温存）==="
crontab -l | grep -E "grove-hb|hb_learning"
