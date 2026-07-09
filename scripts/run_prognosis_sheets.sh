#!/bin/bash
# Hourly: Zoho Прогноз_CF -> Google Sheets (two computed sums).
set -uo pipefail

PROJECT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
cd "$PROJECT_DIR"

PYTHON="${PROJECT_DIR}/.venv/bin/python"
LOG_DIR="${PROJECT_DIR}/logs"
mkdir -p "$LOG_DIR"

LOG="${LOG_DIR}/prognosis_sheets_$(date +%Y%m%d_%H%M%S).log"

log() {
  echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*" | tee -a "$LOG" >&2
}

log "Prognosis -> Sheets start (project=${PROJECT_DIR})"

if ! bash "${PROJECT_DIR}/scripts/check_prognosis_schedule.sh" >>"$LOG" 2>&1; then
  log "Skipped (schedule)"
  exit 0
fi

if [[ ! -x "$PYTHON" ]]; then
  log "ERROR: venv not found: $PYTHON"
  exit 1
fi

if caffeinate -is "$PYTHON" zoho_prognosis_sheets.py run >>"$LOG" 2>&1; then
  log "OK"
  exit 0
else
  code=$?
  log "FAIL exit=${code}"
  exit "$code"
fi
