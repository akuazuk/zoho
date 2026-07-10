#!/bin/bash
# Deploy prognosis job to Google Cloud Run Jobs + Cloud Scheduler (weekdays hourly 8-17 MSK).
#
# Prerequisites:
#   gcloud auth login
#   gcloud config set project carbide-datum-383616
#   Billing enabled (free tier covers this workload)
#
# Usage:
#   cd /path/to/sql_epam
#   bash cloud/deploy_prognosis.sh

set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
cd "$PROJECT_DIR"

if [[ ! -f .env ]]; then
  echo "Missing .env in $PROJECT_DIR" >&2
  exit 1
fi
if [[ ! -f cloud/prognosis-config.env ]]; then
  echo "Missing cloud/prognosis-config.env" >&2
  exit 1
fi

set -a
# shellcheck disable=SC1091
source .env
source cloud/prognosis-config.env
set +a

PROJECT_ID="${GCP_PROJECT_ID:-carbide-datum-383616}"
REGION="${GCP_REGION:-europe-west1}"
JOB_NAME="${PROGNOSIS_JOB_NAME:-prognosis-sheets}"
SCHEDULER_NAME="${PROGNOSIS_SCHEDULER_NAME:-prognosis-sheets-hourly}"
IMAGE="${REGION}-docker.pkg.dev/${PROJECT_ID}/zoho/${JOB_NAME}:latest"
SA_NAME="${PROGNOSIS_RUNNER_SA:-prognosis-runner}"
SA_EMAIL="${SA_NAME}@${PROJECT_ID}.iam.gserviceaccount.com"
SHEETS_KEY="${GOOGLE_SHEETS_CREDENTIALS:-${PROJECT_DIR}/sonorous-saga-321204-35715098b819.json}"

if [[ ! -f "$SHEETS_KEY" ]]; then
  echo "Sheets key not found: $SHEETS_KEY" >&2
  exit 1
fi

echo "=== Project: $PROJECT_ID | Region: $REGION ==="

gcloud config set project "$PROJECT_ID"

echo "=== Enable APIs ==="
gcloud services enable \
  run.googleapis.com \
  cloudbuild.googleapis.com \
  artifactregistry.googleapis.com \
  cloudscheduler.googleapis.com \
  secretmanager.googleapis.com

echo "=== Artifact Registry ==="
gcloud artifacts repositories describe zoho --location="$REGION" >/dev/null 2>&1 \
  || gcloud artifacts repositories create zoho \
    --repository-format=docker \
    --location="$REGION" \
    --description="Zoho automation images"

echo "=== Service account ==="
gcloud iam service-accounts describe "$SA_EMAIL" >/dev/null 2>&1 \
  || gcloud iam service-accounts create "$SA_NAME" \
    --display-name="Prognosis Sheets runner"

gcloud projects add-iam-policy-binding "$PROJECT_ID" \
  --member="serviceAccount:${SA_EMAIL}" \
  --role="roles/secretmanager.secretAccessor" \
  --quiet >/dev/null

gcloud projects add-iam-policy-binding "$PROJECT_ID" \
  --member="serviceAccount:${SA_EMAIL}" \
  --role="roles/run.invoker" \
  --quiet >/dev/null

echo "=== Secrets (Zoho + Sheets key) ==="
if ! gcloud secrets describe prognosis-sheets-sa-key >/dev/null 2>&1; then
  gcloud secrets create prognosis-sheets-sa-key --replication-policy=automatic
fi
gcloud secrets versions add prognosis-sheets-sa-key --data-file="$SHEETS_KEY" --quiet

for name in ZOHO_CLIENT_ID ZOHO_CLIENT_SECRET ZOHO_REFRESH_TOKEN; do
  val="${!name:-}"
  if [[ -z "$val" ]]; then
    echo "Missing $name in .env" >&2
    exit 1
  fi
  secret="prognosis-$(echo "$name" | tr '[:upper:]' '[:lower:]')"
  if ! gcloud secrets describe "$secret" >/dev/null 2>&1; then
    gcloud secrets create "$secret" --replication-policy=automatic
  fi
  printf '%s' "$val" | gcloud secrets versions add "$secret" --data-file=- --quiet
done

for name in ZOHO_ORG_ID ZOHO_WORKSPACE_ID GOOGLE_SHEETS_ID GOOGLE_SHEETS_GID \
  GOOGLE_SHEETS_CELL_CURRENT GOOGLE_SHEETS_CELL_NEXT; do
  val="${!name:-}"
  if [[ -z "$val" ]]; then
    echo "Missing $name in cloud/prognosis-config.env" >&2
    exit 1
  fi
done

echo "=== Build image (Cloud Build) ==="
gcloud builds submit \
  --config=cloud/build-image.cloudbuild.yaml \
  --substitutions="_IMAGE=${IMAGE}" \
  .

echo "=== Cloud Run Job ==="
gcloud run jobs deploy "$JOB_NAME" \
  --image="$IMAGE" \
  --region="$REGION" \
  --service-account="$SA_EMAIL" \
  --max-retries=1 \
  --task-timeout=300s \
  --set-secrets="/secrets/sheets-sa.json=prognosis-sheets-sa-key:latest" \
  --set-secrets="ZOHO_CLIENT_ID=prognosis-zoho_client_id:latest" \
  --set-secrets="ZOHO_CLIENT_SECRET=prognosis-zoho_client_secret:latest" \
  --set-secrets="ZOHO_REFRESH_TOKEN=prognosis-zoho_refresh_token:latest" \
  --set-env-vars="ZOHO_ORG_ID=${ZOHO_ORG_ID}" \
  --set-env-vars="ZOHO_WORKSPACE_ID=${ZOHO_WORKSPACE_ID}" \
  --set-env-vars="GOOGLE_SHEETS_ID=${GOOGLE_SHEETS_ID}" \
  --set-env-vars="GOOGLE_SHEETS_GID=${GOOGLE_SHEETS_GID}" \
  --set-env-vars="GOOGLE_SHEETS_CELL_CURRENT=${GOOGLE_SHEETS_CELL_CURRENT:-A140}" \
  --set-env-vars="GOOGLE_SHEETS_CELL_NEXT=${GOOGLE_SHEETS_CELL_NEXT:-A141}" \
  --set-env-vars="GOOGLE_SHEETS_CREDENTIALS=/secrets/sheets-sa.json" \
  --set-env-vars="PROGNOSIS_SCHEDULE=always"

bash cloud/update_prognosis_scheduler.sh

echo ""
echo "Deployed."
echo "  Job:       $JOB_NAME ($REGION)"
echo "  Schedule:  ${PROGNOSIS_CRON_SCHEDULE} (${PROGNOSIS_TIMEZONE})"
echo "  Test run:  gcloud run jobs execute $JOB_NAME --region=$REGION --wait"
echo ""
echo "After cloud works, stop Mac agent:"
echo "  launchctl unload ~/Library/LaunchAgents/com.kravira.prognosis-hourly.plist"
