#!/bin/bash
# Fallback after morning mis-daily: re-run only if last sync failed or SQLite is behind yesterday.
set -uo pipefail

PROJECT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
cd "$PROJECT_DIR"
PYTHON="${PROJECT_DIR}/.venv/bin/python"
STATUS_FILE="${PROJECT_DIR}/logs/last_sync_status.json"
LOG_DIR="${PROJECT_DIR}/logs"
mkdir -p "$LOG_DIR"
LOG="${LOG_DIR}/sync_retry_$(date +%Y%m%d_%H%M%S).log"

log() { echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*" | tee -a "$LOG" >&2; }

need_retry="$("$PYTHON" - <<'PY'
import json
import sqlite3
from datetime import date, timedelta
from pathlib import Path

status_path = Path("logs/last_sync_status.json")
db_path = Path("data/mis_local.sqlite")
yesterday = date.today() - timedelta(days=1)

need = False
reason = []

if status_path.exists():
    try:
        st = json.loads(status_path.read_text(encoding="utf-8"))
        if not st.get("success"):
            need = True
            reason.append(f"last_sync_failed={st.get('failed_step')}")
    except Exception as e:
        need = True
        reason.append(f"status_unreadable={e}")
else:
    need = True
    reason.append("no_status")

if db_path.exists():
    with sqlite3.connect(db_path) as conn:
        row = conn.execute('SELECT MAX(date("Дата визита")) FROM mis_data').fetchone()
    max_d = row[0] if row and row[0] else None
    if not max_d or max_d < yesterday.isoformat():
        need = True
        reason.append(f"sqlite_max={max_d}<{yesterday}")
else:
    need = True
    reason.append("no_sqlite")

print("YES" if need else "NO")
print("; ".join(reason) if reason else "ok", file=__import__("sys").stderr)
PY
)"

if [[ "$need_retry" != "YES" ]]; then
  log "Skip retry: daily sync already OK and SQLite current"
  exit 0
fi

log "Retry daily sync (MariaDB was likely overloaded at 06:30)"
export MIS_DB_RETRIES="${MIS_DB_RETRIES:-8}"
export MIS_DB_RETRY_DELAY_SEC="${MIS_DB_RETRY_DELAY_SEC:-30}"
bash "${PROJECT_DIR}/scripts/run_daily_sync.sh" >>"$LOG" 2>&1
code=$?
log "Retry finished exit=${code}"
exit "$code"
