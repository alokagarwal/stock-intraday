#!/usr/bin/env bash
# deploy.sh — One-command deploy for the intraday momentum bot
#
# Deploy order:
#   1. Validate env vars
#   2. Bootstrap S3 bucket (idempotent)
#   3. Build Lambda zips (linux/x86_64 wheels — avoids macOS/ARM mismatch)
#   4. Upload zips to S3
#   5. terraform init (S3 backend)
#   6. terraform apply
#   7. Write Alpaca credentials to Secrets Manager (never in TF state)
#   8. Set GitHub Actions secrets (via gh CLI if available)
#   9. Cleanup local zips
#
# Usage:
#   export ALPACA_API_KEY="PK..."
#   export ALPACA_SECRET_KEY="..."
#   ./deploy.sh --email you@example.com          # paper trading (default)
#   ./deploy.sh --email you@example.com --live   # REAL MONEY

set -euo pipefail

# ── Argument parsing ──────────────────────────────────────────
MODE="paper"
ALERT_EMAIL=""
while [[ $# -gt 0 ]]; do
  case $1 in
    --live)  MODE="live";  shift ;;
    --paper) MODE="paper"; shift ;;
    --email) ALERT_EMAIL="$2"; shift 2 ;;
    *) echo "Unknown argument: $1"; exit 1 ;;
  esac
done

if [[ $MODE == "live" ]]; then
  ALPACA_URL="https://api.alpaca.markets"
  echo ""
  echo "WARNING: LIVE TRADING MODE — real money will be at risk"
  read -rp "Type LIVE to confirm: " CONFIRM
  [[ "$CONFIRM" != "LIVE" ]] && echo "Aborted." && exit 1
else
  ALPACA_URL="https://paper-api.alpaca.markets"
  echo "Paper trading mode"
fi

# ── Validate prerequisites ────────────────────────────────────
[[ -z "${ALPACA_API_KEY:-}"    ]] && echo "ALPACA_API_KEY not set"    && exit 1
[[ -z "${ALPACA_SECRET_KEY:-}" ]] && echo "ALPACA_SECRET_KEY not set" && exit 1
command -v terraform >/dev/null || { echo "terraform not found"; exit 1; }
command -v aws       >/dev/null || { echo "aws CLI not found";   exit 1; }
command -v pip       >/dev/null || { echo "pip not found";       exit 1; }

AWS_REGION="${AWS_REGION:-us-east-1}"
ACCOUNT=$(aws sts get-caller-identity --query Account --output text)
BUCKET="momentum-bot-${ACCOUNT}-${AWS_REGION}"

echo ""
echo "================================================================"
echo " Account : ${ACCOUNT}"
echo " Region  : ${AWS_REGION}"
echo " Bucket  : ${BUCKET}"
echo " Mode    : ${MODE}"
echo " Email   : ${ALERT_EMAIL:-<none>}"
echo "================================================================"

# ── Step 1: Bootstrap S3 ─────────────────────────────────────
echo ""
echo "Step 1/7 — Bootstrap S3 bucket"
bash infrastructure/bootstrap.sh "${AWS_REGION}"

# ── Step 2: Build Lambda zips ─────────────────────────────────
echo ""
echo "Step 2/7 — Build Lambda zips (linux/x86_64)"
echo "  Note: --platform manylinux2014_x86_64 downloads Linux wheels"
echo "  regardless of host OS — fixes pydantic_core mismatch on macOS/ARM"

TMPDIR_BUILD=$(mktemp -d)
PKG="${TMPDIR_BUILD}/pkg"
mkdir -p "${PKG}"

pip install \
  --platform manylinux2014_x86_64 \
  --python-version 3.11 \
  --implementation cp \
  --target "${PKG}" \
  --quiet \
  "alpaca-py>=0.28.0" \
  "requests>=2.32.0" \
  "pytz>=2024.1"
# Note: --only-binary=:all: is intentionally omitted.
# It skips pure-Python packages like pytz that have no binary wheel,
# causing silent ModuleNotFoundError crashes inside Lambda.

# Trim zip: remove test dirs, __pycache__, dist-info
find "${PKG}" -type d -name "__pycache__" -exec rm -rf {} + 2>/dev/null || true
find "${PKG}" -type d -name "*.dist-info" -exec rm -rf {} + 2>/dev/null || true
find "${PKG}" -type d -name "tests"       -exec rm -rf {} + 2>/dev/null || true

# Copy shared modules into the package
cp config.py trader.py watchlist_db.py signal_engine.py risk_guard.py "${PKG}/"

# Build monitor zip
cp lambdas/intraday_monitor.py "${PKG}/"
(cd "${PKG}" && zip -r9q /tmp/lambda_monitor.zip .)
cp /tmp/lambda_monitor.zip ./lambda_monitor.zip
MONITOR_HASH=$(openssl dgst -sha256 -binary lambda_monitor.zip | openssl enc -base64)

# Build eod zip
cp lambdas/eod_seller.py "${PKG}/"
(cd "${PKG}" && zip -r9q /tmp/lambda_eod.zip .)
cp /tmp/lambda_eod.zip ./lambda_eod.zip
EOD_HASH=$(openssl dgst -sha256 -binary lambda_eod.zip | openssl enc -base64)

rm -rf "${TMPDIR_BUILD}"
echo "  monitor: $(du -sh lambda_monitor.zip | cut -f1)"
echo "  eod    : $(du -sh lambda_eod.zip     | cut -f1)"

# ── Step 3: Upload zips to S3 ─────────────────────────────────
echo ""
echo "Step 3/7 — Upload Lambda zips to S3"
aws s3 cp lambda_monitor.zip "s3://${BUCKET}/lambdas/lambda_monitor.zip" --region "${AWS_REGION}"
aws s3 cp lambda_eod.zip     "s3://${BUCKET}/lambdas/lambda_eod.zip"     --region "${AWS_REGION}"
echo "  Uploaded to s3://${BUCKET}/lambdas/"

# ── Step 4: Terraform init ────────────────────────────────────
echo ""
echo "Step 4/7 — Terraform init"
cd infrastructure
terraform init -reconfigure -input=false \
  -backend-config="bucket=${BUCKET}" \
  -backend-config="key=momentum-bot/terraform.tfstate" \
  -backend-config="region=${AWS_REGION}"

# ── Step 5: Terraform apply ───────────────────────────────────
echo ""
echo "Step 5/7 — Terraform apply"
terraform apply -auto-approve -input=false \
  -var="aws_region=${AWS_REGION}" \
  -var="alert_email=${ALERT_EMAIL}" \
  -var="monitor_zip_hash=${MONITOR_HASH}" \
  -var="eod_zip_hash=${EOD_HASH}"

# Capture outputs
GH_KEY_ID=$(terraform output -raw github_access_key_id)
GH_SECRET=$(terraform output -raw github_secret_access_key)
SNS_ARN=$(terraform output -raw sns_topic_arn)
SECRETS_ARN=$(terraform output -raw secrets_arn)
cd ..

# ── Step 6: Write Alpaca credentials to Secrets Manager ───────
echo ""
echo "Step 6/7 — Writing Alpaca credentials to Secrets Manager"
echo "  (credentials never stored in Terraform state or plan output)"

aws secretsmanager put-secret-value \
  --secret-id "${SECRETS_ARN}" \
  --secret-string "{
    \"ALPACA_API_KEY\":    \"${ALPACA_API_KEY}\",
    \"ALPACA_SECRET_KEY\": \"${ALPACA_SECRET_KEY}\",
    \"ALPACA_BASE_URL\":   \"${ALPACA_URL}\"
  }" \
  --region "${AWS_REGION}" \
  --output text --query VersionId > /dev/null

echo "  Credentials written to ${SECRETS_ARN}"

# ── Step 7: Set GitHub secrets + cleanup ──────────────────────
echo ""
echo "Step 7/7 — Cleanup and GitHub secrets"

rm -f ./lambda_monitor.zip ./lambda_eod.zip
echo "  Local zip files removed"

GITHUB_SECRETS=(
  "AWS_ACCESS_KEY_ID=${GH_KEY_ID}"
  "AWS_SECRET_ACCESS_KEY=${GH_SECRET}"
  "AWS_REGION=${AWS_REGION}"
  "SNS_TOPIC_ARN=${SNS_ARN}"
  "SECRETS_ARN=${SECRETS_ARN}"
  "ALPACA_API_KEY=${ALPACA_API_KEY}"
  "ALPACA_SECRET_KEY=${ALPACA_SECRET_KEY}"
  "ALPACA_BASE_URL=${ALPACA_URL}"
)

if command -v gh >/dev/null 2>&1; then
  REPO="${GITHUB_REPOSITORY:-}"
  if [[ -z "$REPO" ]]; then
    REMOTE_URL=$(git config --get remote.origin.url 2>/dev/null || true)
    if [[ $REMOTE_URL =~ github.com[:/](.+/[^/.]+)(\.git)?$ ]]; then
      REPO="${BASH_REMATCH[1]}"
    fi
  fi

  if [[ -n "$REPO" ]]; then
    echo "  Setting GitHub secrets in ${REPO} via gh CLI"
    for pair in "${GITHUB_SECRETS[@]}"; do
      KEY="${pair%%=*}"
      VAL="${pair#*=}"
      gh secret set "${KEY}" --body "${VAL}" -R "${REPO}"
    done
    echo "  GitHub secrets set"
  else
    echo "  gh CLI found but repo not determined — set secrets manually (see below)"
  fi
else
  echo "  gh CLI not installed — set GitHub secrets manually:"
fi

# Always print them for manual reference
echo ""
echo "================================================================"
echo " DEPLOY COMPLETE"
echo "================================================================"
echo ""
echo " GitHub Actions secrets to set:"
echo " (Settings -> Secrets and variables -> Actions)"
echo ""
for pair in "${GITHUB_SECRETS[@]}"; do
  KEY="${pair%%=*}"
  echo "   ${KEY}"
done
echo ""
echo " Terraform state: s3://${BUCKET}/momentum-bot/terraform.tfstate"
echo " Secrets Manager: ${SECRETS_ARN}"
echo ""
echo " Test commands:"
echo "   aws lambda invoke --function-name momentum-bot-monitor /tmp/out.json && cat /tmp/out.json"
echo "   aws lambda invoke --function-name momentum-bot-eod-seller /tmp/out.json && cat /tmp/out.json"
echo ""
echo " Run the scanner now (don't wait for GitHub Actions):"
echo "   pip install -r requirements-scanner.txt"
echo "   python scanner_task/pre_market_scanner.py"
echo ""
echo " To rotate Alpaca keys later:"
echo "   aws secretsmanager put-secret-value \\"
echo "     --secret-id ${SECRETS_ARN} \\"
echo "     --secret-string '{\"ALPACA_API_KEY\":\"NEW\",\"ALPACA_SECRET_KEY\":\"NEW\",\"ALPACA_BASE_URL\":\"${ALPACA_URL}\"}'"
echo "================================================================"
