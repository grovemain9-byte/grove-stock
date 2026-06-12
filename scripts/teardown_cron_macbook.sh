#!/usr/bin/env bash
# MacBook側 grove-stock cron を停止
# SSH で MacBook に接続して実行 (mini→macbook)
set -euo pipefail

echo "=== stopping grove-stock cron on MacBook ==="
ssh macbook 'bash -s' <<'REMOTE'
set -e
TMP=$(mktemp)
crontab -l 2>/dev/null > "$TMP" || true
echo "--- before (grove-stock lines) ---"
grep "grove-stock" "$TMP" || echo "(none found)"

# grove-stock関連行をコメントアウト (復帰できるよう削除せず)
sed -i.bak 's|^\*/30.*grove-stock.*|# RETIRED-MINI '"$(date +%F)"' &|' "$TMP"
sed -i.bak 's|^0,30.*grove-stock.*|# RETIRED-MINI '"$(date +%F)"' &|' "$TMP"
sed -i.bak 's|^# === grove-stock BNF Swing.*|# RETIRED-MINI '"$(date +%F)"' &|' "$TMP"

crontab "$TMP"
rm -f "$TMP" "$TMP.bak"
echo "--- after ---"
crontab -l | grep -i "grove-stock\|RETIRED-MINI" | head
REMOTE
echo "=== done ==="
