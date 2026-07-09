#!/bin/bash
# Daily: MariaDB -> SQLite -> Zoho Analytics (BigQuery sync is manual only).
set -uo pipefail

PROJECT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
cd "$PROJECT_DIR"

PYTHON="${PROJECT_DIR}/.venv/bin/python"
LOG_DIR="${PROJECT_DIR}/logs"
STATUS_FILE="${LOG_DIR}/last_sync_status.json"
mkdir -p "$LOG_DIR" "${PROJECT_DIR}/data/zoho_sync"

TS_START="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
LOG="${LOG_DIR}/sync_$(date +%Y%m%d_%H%M%S).log"

STEP_MARIADB_OK=false
STEP_MARIADB_CODE=0
STEP_MARIADB_SEC=0

STEP_ZOHO_OK=false
STEP_ZOHO_CODE=0
STEP_ZOHO_SEC=0

FAILED_STEP=""
OVERALL_OK=false

log() {
  # stderr + log file (stdout reserved for run_step return value)
  echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*" | tee -a "$LOG" >&2
}

run_step() {
  local name="$1"
  shift
  log "=== ${name} ==="
  local t0=$SECONDS
  caffeinate -is "$@" >>"$LOG" 2>&1
  local code=$?
  local elapsed=$((SECONDS - t0))
  if [[ $code -eq 0 ]]; then
    log "OK ${name} (${elapsed}s)"
  else
    log "FAIL ${name} exit=${code} (${elapsed}s)"
  fi
  printf '%s:%s' "$code" "$elapsed"
}

write_status() {
  STATUS_OVERALL_OK="$OVERALL_OK" \
  STATUS_FAILED_STEP="$FAILED_STEP" \
  STATUS_TS_START="$TS_START" \
  STATUS_LOG_FILE="${LOG#${PROJECT_DIR}/}" \
  STATUS_MARIADB_OK="$STEP_MARIADB_OK" \
  STATUS_MARIADB_CODE="$STEP_MARIADB_CODE" \
  STATUS_MARIADB_SEC="$STEP_MARIADB_SEC" \
  STATUS_ZOHO_OK="$STEP_ZOHO_OK" \
  STATUS_ZOHO_CODE="$STEP_ZOHO_CODE" \
  STATUS_ZOHO_SEC="$STEP_ZOHO_SEC" \
  STATUS_FILE_PATH="$STATUS_FILE" \
  "$PYTHON" - <<'PY'
import json
import os
from datetime import datetime, timezone
from pathlib import Path

def b(name: str) -> bool:
    return os.environ.get(name, "false") == "true"

data = {
    "started_at": os.environ["STATUS_TS_START"],
    "finished_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    "success": b("STATUS_OVERALL_OK"),
    "failed_step": os.environ.get("STATUS_FAILED_STEP") or None,
    "log_file": os.environ["STATUS_LOG_FILE"],
    "steps": {
        "mariadb_sqlite": {
            "ok": b("STATUS_MARIADB_OK"),
            "exit_code": int(os.environ["STATUS_MARIADB_CODE"]),
            "duration_sec": int(os.environ["STATUS_MARIADB_SEC"]),
        },
        "zoho": {
            "ok": b("STATUS_ZOHO_OK"),
            "exit_code": int(os.environ["STATUS_ZOHO_CODE"]),
            "duration_sec": int(os.environ["STATUS_ZOHO_SEC"]),
        },
    },
}
Path(os.environ["STATUS_FILE_PATH"]).write_text(
    json.dumps(data, ensure_ascii=False, indent=2) + "\n",
    encoding="utf-8",
)
PY
}

trap 'write_status' EXIT

log "Daily sync start (project=${PROJECT_DIR})"

if [[ ! -x "$PYTHON" ]]; then
  log "ERROR: venv not found: $PYTHON"
  FAILED_STEP="setup"
  exit 1
fi

result=$(run_step "MariaDB -> SQLite" "$PYTHON" sync_mis_data.py run)
STEP_MARIADB_CODE="${result%%:*}"
STEP_MARIADB_SEC="${result##*:}"
if [[ "$STEP_MARIADB_CODE" -eq 0 ]]; then
  STEP_MARIADB_OK=true
else
  FAILED_STEP="mariadb_sqlite"
  log "Aborting: MariaDB sync failed"
  exit "$STEP_MARIADB_CODE"
fi

result=$(run_step "SQLite -> Zoho Analytics" "$PYTHON" zoho_sync.py run)
STEP_ZOHO_CODE="${result%%:*}"
STEP_ZOHO_SEC="${result##*:}"
if [[ "$STEP_ZOHO_CODE" -eq 0 ]]; then
  STEP_ZOHO_OK=true
  OVERALL_OK=true
  log "Daily sync completed successfully"
  exit 0
else
  FAILED_STEP="zoho"
  log "Daily sync failed at Zoho step"
  exit "$STEP_ZOHO_CODE"
fi
