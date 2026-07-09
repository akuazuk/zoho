#!/bin/bash
# Schedule one-time backfill tonight at 00:30 (Mac must wake at 00:25).
#
# Usage:
#   ./scripts/schedule_backfill_tonight.sh
#   ./scripts/schedule_backfill_tonight.sh 2026-06-09   # explicit wake date (day of 00:30)

set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
PLIST_SRC="$PROJECT_DIR/scripts/com.kravira.mis-backfill.plist"
PLIST_DST="$HOME/Library/LaunchAgents/com.kravira.mis-backfill.plist"
RUN_SCRIPT="$PROJECT_DIR/scripts/run_backfill.sh"

WAKE_DATE="${1:-$(date -v+1d +%m/%d/%Y)}"   # tomorrow by default
WAKE_TIME="00:25:00"
RUN_TIME="00:30"

chmod +x "$RUN_SCRIPT"

mkdir -p "$PROJECT_DIR/logs" "$PROJECT_DIR/data"
mkdir -p "$HOME/Library/LaunchAgents"

cp "$PLIST_SRC" "$PLIST_DST"
# Replace placeholder paths (in case project moved)
sed -i '' "s|__PROJECT_DIR__|$PROJECT_DIR|g" "$PLIST_DST"

launchctl bootout "gui/$(id -u)" "$PLIST_DST" 2>/dev/null || true
launchctl bootstrap "gui/$(id -u)" "$PLIST_DST" 2>/dev/null || launchctl load "$PLIST_DST"

echo "=== Scheduled ==="
echo "LaunchAgent : $PLIST_DST"
echo "Runs daily at: $RUN_TIME (script exits immediately if backfill already done)"
echo ""
echo "IMPORTANT: Mac must WAKE before 00:30. Run this (needs password):"
echo "  sudo pmset schedule wake \"$WAKE_DATE $WAKE_TIME\""
echo ""
echo "Check scheduled wake:"
echo "  pmset -g sched"
echo ""
echo "Cancel wake if needed:"
echo "  sudo pmset schedule cancelall"
echo ""
echo "Remove scheduler after backfill (or it will no-op thanks to .backfill_done):"
echo "  launchctl bootout gui/$(id -u) $PLIST_DST"
echo ""
echo "Logs after run:"
echo "  ls -lt $PROJECT_DIR/logs/backfill_*.log | head"
