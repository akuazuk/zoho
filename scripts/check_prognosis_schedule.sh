#!/bin/bash
# Exit 0 = run now, 1 = skip (outside configured window).
# PROGNOSIS_SCHEDULE: always (default) | weekdays_8_18

set -euo pipefail

schedule="${PROGNOSIS_SCHEDULE:-always}"

case "$schedule" in
  always)
    exit 0
    ;;
  weekdays_8_18)
    dow="$(date +%u)"   # 1=Mon .. 7=Sun
    hour="$(date +%H)"  # 00-23
    if [[ "$dow" -le 5 && "$hour" -ge 8 && "$hour" -lt 18 ]]; then
      exit 0
    fi
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] Skip: outside weekdays 08:00-18:00 (dow=${dow}, hour=${hour})"
    exit 1
    ;;
  *)
    echo "Unknown PROGNOSIS_SCHEDULE=${schedule}" >&2
    exit 2
    ;;
esac
