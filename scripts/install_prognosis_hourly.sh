#!/bin/bash
# Install hourly LaunchAgent for Zoho Прогноз_CF -> Google Sheets.
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
PLIST_SRC="${PROJECT_DIR}/scripts/com.kravira.prognosis-hourly.plist"
PLIST_DST="${HOME}/Library/LaunchAgents/com.kravira.prognosis-hourly.plist"

cp "$PLIST_SRC" "$PLIST_DST"
launchctl unload "$PLIST_DST" 2>/dev/null || true
launchctl load "$PLIST_DST"
launchctl list | grep com.kravira.prognosis-hourly || true

echo "Installed: $PLIST_DST"
echo "Runs every hour (StartInterval=3600), weekdays 08:00-18:00 only (PROGNOSIS_SCHEDULE=weekdays_8_18)."
