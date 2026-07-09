#!/bin/bash
# Connect GitHub repo to Cloud Build and create push trigger (step 3).
# Run after first deploy_prognosis.sh and git push to main.
#
# Usage:
#   bash cloud/setup_github_trigger.sh

set -euo pipefail

PROJECT_ID="${GCP_PROJECT_ID:-carbide-datum-383616}"
REGION="${GCP_REGION:-europe-west1}"
REPO_OWNER="${GITHUB_REPO_OWNER:-akuazuk}"
REPO_NAME="${GITHUB_REPO_NAME:-zoho}"
BRANCH="${GITHUB_BRANCH:-^main$}"
TRIGGER_NAME="${CLOUD_BUILD_TRIGGER:-prognosis-sheets-push}"
CONNECTION_NAME="${GITHUB_CONNECTION:-github-zoho}"
REPO_LINK="${GITHUB_REPO_LINK:-${REPO_OWNER}-${REPO_NAME}}"

gcloud config set project "$PROJECT_ID"

echo "=== GitHub connection (2nd gen) ==="
if ! gcloud builds connections describe "$CONNECTION_NAME" --region="$REGION" >/dev/null 2>&1; then
  echo "Create GitHub connection — open the URL below and authorize:"
  gcloud builds connections create github "$CONNECTION_NAME" \
    --region="$REGION" \
    --authorizer-token="$(gcloud auth print-access-token)"
fi

if ! gcloud builds repositories describe "$REPO_LINK" \
  --connection="$CONNECTION_NAME" --region="$REGION" >/dev/null 2>&1; then
  gcloud builds repositories create "$REPO_LINK" \
    --connection="$CONNECTION_NAME" \
    --region="$REGION" \
    --remote-uri="https://github.com/${REPO_OWNER}/${REPO_NAME}.git"
fi

echo "=== Cloud Build trigger on push to main ==="
if gcloud builds triggers describe "$TRIGGER_NAME" --region="$REGION" >/dev/null 2>&1; then
  gcloud builds triggers update github "$TRIGGER_NAME" \
    --region="$REGION" \
    --repository="projects/${PROJECT_ID}/locations/${REGION}/connections/${CONNECTION_NAME}/repositories/${REPO_LINK}" \
    --branch-pattern="$BRANCH" \
    --build-config=cloudbuild.yaml
else
  gcloud builds triggers create github "$TRIGGER_NAME" \
    --region="$REGION" \
    --repository="projects/${PROJECT_ID}/locations/${REGION}/connections/${CONNECTION_NAME}/repositories/${REPO_LINK}" \
    --branch-pattern="$BRANCH" \
    --build-config=cloudbuild.yaml \
    --description="Rebuild prognosis-sheets on push to main"
fi

echo "Done. Push to main will run cloudbuild.yaml automatically."
