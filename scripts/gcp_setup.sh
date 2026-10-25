#!/usr/bin/env bash
# One-time GCP setup for Deal Desk Agent. Run it in Cloud Shell (gcloud and bq are preinstalled):
#
#   PROJECT_ID=my-project BILLING_ACCOUNT=XXXXXX-XXXXXX-XXXXXX bash scripts/gcp_setup.sh
#
# Safe to re-run: every step skips what already exists. It prompts for the Salesforce and
# Anthropic secrets (input hidden) and never prints them.
set -euo pipefail

: "${PROJECT_ID:?set PROJECT_ID}"
: "${BILLING_ACCOUNT:?set BILLING_ACCOUNT (gcloud billing accounts list)}"
REGION="${REGION:-europe-west1}"          # Cloud Run free tier applies in tier-1 regions
BQ_LOCATION="${BQ_LOCATION:-EU}"
GITHUB_REPO="${GITHUB_REPO:-drPolPat/deal-desk-agent}"
BUDGET="${BUDGET:-5}"                     # monthly budget alert, in the billing account's currency
SERVICE=deal-desk-agent
RUNTIME_SA="deal-desk-runtime@${PROJECT_ID}.iam.gserviceaccount.com"
DEPLOY_SA="deal-desk-deployer@${PROJECT_ID}.iam.gserviceaccount.com"

gcloud config set project "$PROJECT_ID" >/dev/null
PROJECT_NUMBER=$(gcloud projects describe "$PROJECT_ID" --format='value(projectNumber)')

echo "== APIs"
gcloud services enable run.googleapis.com artifactregistry.googleapis.com secretmanager.googleapis.com \
  bigquery.googleapis.com iamcredentials.googleapis.com sts.googleapis.com billingbudgets.googleapis.com

echo "== Budget alert (${BUDGET}/month, emails at 50/90/100%)"
if ! gcloud billing budgets list --billing-account="$BILLING_ACCOUNT" --format='value(displayName)' \
    | grep -qx "$SERVICE"; then
  gcloud billing budgets create --billing-account="$BILLING_ACCOUNT" --display-name="$SERVICE" \
    --budget-amount="$BUDGET" --filter-projects="projects/$PROJECT_NUMBER" \
    --threshold-rule=percent=0.5 --threshold-rule=percent=0.9 --threshold-rule=percent=1.0
fi

echo "== Artifact Registry (keeps the 3 newest images to stay inside the free storage tier)"
if ! gcloud artifacts repositories describe "$SERVICE" --location="$REGION" >/dev/null 2>&1; then
  gcloud artifacts repositories create "$SERVICE" --repository-format=docker --location="$REGION"
fi
POLICY=$(mktemp)
cat >"$POLICY" <<'EOF'
[{"name": "keep-3", "action": {"type": "Keep"}, "mostRecentVersions": {"keepCount": 3}},
 {"name": "delete-rest", "action": {"type": "Delete"}, "condition": {"tagState": "any"}}]
EOF
gcloud artifacts repositories set-cleanup-policies "$SERVICE" --location="$REGION" --policy="$POLICY" --no-dry-run
rm -f "$POLICY"

echo "== Service accounts"
for sa in deal-desk-runtime deal-desk-deployer; do
  gcloud iam service-accounts describe "${sa}@${PROJECT_ID}.iam.gserviceaccount.com" >/dev/null 2>&1 \
    || gcloud iam service-accounts create "$sa" --display-name="$sa"
done

echo "== Secrets (input hidden; press Enter to keep an existing value)"
put_secret() {  # name, prompt
  local value
  read -rsp "$2: " value; echo
  if ! gcloud secrets describe "$1" >/dev/null 2>&1; then
    [ -n "$value" ] || { echo "  $1 is required"; exit 1; }
    printf %s "$value" | gcloud secrets create "$1" --replication-policy=automatic --data-file=-
  elif [ -n "$value" ]; then
    printf %s "$value" | gcloud secrets versions add "$1" --data-file=-
  fi
  # The runtime service account can read only these secrets, nothing else in the project.
  gcloud secrets add-iam-policy-binding "$1" --member="serviceAccount:$RUNTIME_SA" \
    --role=roles/secretmanager.secretAccessor >/dev/null
}
put_secret sf-client-id "Salesforce consumer key (SF_CLIENT_ID)"
put_secret sf-client-secret "Salesforce consumer secret (SF_CLIENT_SECRET)"
put_secret anthropic-api-key "Anthropic API key"
if ! gcloud secrets describe review-api-token >/dev/null 2>&1; then
  openssl rand -hex 32 | tr -d '\n' | gcloud secrets create review-api-token --replication-policy=automatic --data-file=-
  echo "  generated review-api-token (read it with: gcloud secrets versions access latest --secret review-api-token)"
fi
gcloud secrets add-iam-policy-binding review-api-token --member="serviceAccount:$RUNTIME_SA" \
  --role=roles/secretmanager.secretAccessor >/dev/null

echo "== BigQuery dataset (the service creates its tables on first start)"
bq --location="$BQ_LOCATION" show --dataset "${PROJECT_ID}:deal_desk" >/dev/null 2>&1 \
  || bq --location="$BQ_LOCATION" mk --dataset "${PROJECT_ID}:deal_desk"
# Runtime SA: write/read only this dataset, plus permission to run query jobs.
if ! bq add-iam-policy-binding --member="serviceAccount:$RUNTIME_SA" --role=roles/bigquery.dataEditor \
    "${PROJECT_ID}:deal_desk" >/dev/null 2>&1; then
  echo "  (this bq version can't bind dataset IAM; granting dataEditor at project level instead)"
  gcloud projects add-iam-policy-binding "$PROJECT_ID" --member="serviceAccount:$RUNTIME_SA" \
    --role=roles/bigquery.dataEditor --condition=None >/dev/null
fi
gcloud projects add-iam-policy-binding "$PROJECT_ID" --member="serviceAccount:$RUNTIME_SA" \
  --role=roles/bigquery.jobUser --condition=None >/dev/null

echo "== Deployer permissions (GitHub Actions, no keys: Workload Identity Federation)"
gcloud projects add-iam-policy-binding "$PROJECT_ID" --member="serviceAccount:$DEPLOY_SA" \
  --role=roles/run.admin --condition=None >/dev/null
gcloud artifacts repositories add-iam-policy-binding "$SERVICE" --location="$REGION" \
  --member="serviceAccount:$DEPLOY_SA" --role=roles/artifactregistry.writer >/dev/null
gcloud iam service-accounts add-iam-policy-binding "$RUNTIME_SA" \
  --member="serviceAccount:$DEPLOY_SA" --role=roles/iam.serviceAccountUser >/dev/null

if ! gcloud iam workload-identity-pools describe github --location=global >/dev/null 2>&1; then
  gcloud iam workload-identity-pools create github --location=global --display-name="GitHub Actions"
fi
if ! gcloud iam workload-identity-pools providers describe github --workload-identity-pool=github \
    --location=global >/dev/null 2>&1; then
  # Only workflows from this one repository can obtain tokens.
  gcloud iam workload-identity-pools providers create-oidc github --workload-identity-pool=github \
    --location=global --issuer-uri="https://token.actions.githubusercontent.com" \
    --attribute-mapping="google.subject=assertion.sub,attribute.repository=assertion.repository" \
    --attribute-condition="assertion.repository == '${GITHUB_REPO}'"
fi
gcloud iam service-accounts add-iam-policy-binding "$DEPLOY_SA" --role=roles/iam.workloadIdentityUser \
  --member="principalSet://iam.googleapis.com/projects/${PROJECT_NUMBER}/locations/global/workloadIdentityPools/github/attribute.repository/${GITHUB_REPO}" \
  >/dev/null

cat <<EOF

Done. Add these as GitHub repository VARIABLES (Settings > Secrets and variables > Actions > Variables):

  GCP_PROJECT_ID   = ${PROJECT_ID}
  GCP_REGION       = ${REGION}
  WIF_PROVIDER     = projects/${PROJECT_NUMBER}/locations/global/workloadIdentityPools/github/providers/github
  DEPLOY_SA        = ${DEPLOY_SA}
  RUNTIME_SA       = ${RUNTIME_SA}
  SF_DOMAIN        = <your My Domain host, e.g. orgfarm-xxxx-dev-ed.develop.my.salesforce.com>
  REVIEWER_NAME    = <name recorded as approver>
  DRY_RUN          = true      (set to false when you want the deployed service to write Tasks)

The next push to main deploys the service.
EOF
