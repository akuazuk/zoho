#!/bin/bash
# Schedule Mac wake at 06:25 daily (5 min before com.kravira.mis-daily at 06:30).
# Requires administrator privileges (sudo or macOS password prompt).
set -euo pipefail

WAKE_TIME="06:25:00"
DAYS="MTWRFSU"

if [[ "$(uname -s)" != "Darwin" ]]; then
  echo "This script is for macOS only." >&2
  exit 1
fi

_run_pmset() {
  pmset repeat cancel 2>/dev/null || true
  pmset repeat wakeorpoweron "${DAYS}" "${WAKE_TIME}"
  echo
  pmset -g sched
}

if [[ "${EUID:-$(id -u)}" -eq 0 ]]; then
  _run_pmset
elif command -v osascript >/dev/null 2>&1; then
  echo "Requesting administrator privileges..."
  osascript -e "do shell script \"pmset repeat cancel 2>/dev/null; pmset repeat wakeorpoweron ${DAYS} ${WAKE_TIME}; pmset -g sched\" with administrator privileges"
else
  echo "Run with sudo: sudo bash $0" >&2
  exit 1
fi

echo
echo "Wake scheduled: ${WAKE_TIME} every day (${DAYS})"
