#!/bin/bash
# Apply Cloud Scheduler settings from cloud/prognosis-config.env
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
cd "$PROJECT_DIR"

set -a
# shellcheck disable=SC1091
source cloud/prognosis-config.env
set +a

PROJECT_ID="${GCP_PROJECT_ID:-carbide-datum-383616}"
REGION="${GCP_REGION:-europe-west1}"
JOB_NAME="${PROGNOSIS_JOB_NAME:-prognosis-sheets}"
SCHEDULER_NAME="${PROGNOSIS_SCHEDULER_NAME:-prognosis-sheets-hourly}"
SA_NAME="${PROGNOSIS_RUNNER_SA:-prognosis-runner}"
SA_EMAIL="${SA_NAME}@${PROJECT_ID}.iam.gserviceaccount.com"
CRON="${PROGNOSIS_CRON_SCHEDULE:-0 8-17 * * 1-5}"
TZ="${PROGNOSIS_TIMEZONE:-Europe/Moscow}"

RUN_URI="https://${REGION}-run.googleapis.com/apis/run.googleapis.com/v1/namespaces/${PROJECT_ID}/jobs/${JOB_NAME}:run"

echo "Cloud Scheduler: ${SCHEDULER_NAME}"
echo "  cron:     ${CRON}"
echo "  timezone: ${TZ}"

if gcloud scheduler jobs describe "$SCHEDULER_NAME" --location="$REGION" --project="$PROJECT_ID" >/dev/null 2>&1; then
  gcloud scheduler jobs update http "$SCHEDULER_NAME" \
    --location="$REGION" \
    --project="$PROJECT_ID" \
    --schedule="$CRON" \
    --time-zone="$TZ" \
    --uri="$RUN_URI" \
    --http-method=POST \
    --oauth-service-account-email="$SA_EMAIL" \
    --oauth-token-scope="https://www.googleapis.com/auth/cloud-platform"
else
  gcloud scheduler jobs create http "$SCHEDULER_NAME" \
    --location="$REGION" \
    --project="$PROJECT_ID" \
    --schedule="$CRON" \
    --time-zone="$TZ" \
    --uri="$RUN_URI" \
    --http-method=POST \
    --oauth-service-account-email="$SA_EMAIL" \
    --oauth-token-scope="https://www.googleapis.com/auth/cloud-platform"
fi
