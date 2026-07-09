#!/bin/bash
# Primary backfill: month-by-month until MIS_BACKFILL_UNTIL.
# Keeps Mac awake while running (caffeinate).

set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
cd "$PROJECT_DIR"

mkdir -p logs
LOG="logs/backfill_$(date +%Y%m%d_%H%M%S).log"
DONE_FLAG="data/.backfill_done"

log() { echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*" | tee -a "$LOG"; }

if [[ -f "$DONE_FLAG" ]]; then
  log "Backfill already completed ($DONE_FLAG exists). Exit."
  exit 0
fi

if [[ ! -f .env ]]; then
  log "ERROR: .env not found in $PROJECT_DIR"
  exit 1
fi

if [[ ! -x .venv/bin/python ]]; then
  log "ERROR: .venv not found. Run: python3 -m venv .venv && pip install -r requirements.txt"
  exit 1
fi

log "Start backfill from $PROJECT_DIR"
log "Log file: $LOG"

# -i: prevent idle sleep while script runs (important after night wake)
# -s: prevent system sleep (when on AC power)
caffeinate -is .venv/bin/python sync_mis_data.py backfill >>"$LOG" 2>&1
status=$?

if [[ $status -eq 0 ]]; then
  touch "$DONE_FLAG"
  log "Backfill OK. Marker: $DONE_FLAG"
else
  log "Backfill FAILED with exit code $status"
  exit "$status"
fi

# One-time LaunchAgent: remove after success
PLIST="$HOME/Library/LaunchAgents/com.kravira.mis-backfill.plist"
if [[ -f "$PLIST" ]]; then
  launchctl bootout "gui/$(id -u)" "$PLIST" 2>/dev/null || launchctl unload "$PLIST" 2>/dev/null || true
  log "LaunchAgent unloaded (one-time job done)."
fi

log "Finished."
