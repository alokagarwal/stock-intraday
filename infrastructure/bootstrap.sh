#!/usr/bin/env bash
# infrastructure/bootstrap.sh
#
# Creates the S3 bucket that holds both Terraform state and Lambda zips.
# Must run BEFORE terraform init. Idempotent — safe to re-run.
#
# Usage:
#   bash infrastructure/bootstrap.sh [aws_region]

set -euo pipefail
REGION="${1:-us-east-1}"
ACCOUNT=$(aws sts get-caller-identity --query Account --output text)
BUCKET="momentum-bot-${ACCOUNT}-${REGION}"

echo "  Bootstrap: checking bucket ${BUCKET}"

if aws s3api head-bucket --bucket "${BUCKET}" 2>/dev/null; then
  echo "  Bucket already exists — OK"
else
  echo "  Creating bucket ${BUCKET} in ${REGION}"
  if [[ "${REGION}" == "us-east-1" ]]; then
    aws s3api create-bucket \
      --bucket "${BUCKET}" \
      --region "${REGION}"
  else
    aws s3api create-bucket \
      --bucket "${BUCKET}" \
      --region "${REGION}" \
      --create-bucket-configuration LocationConstraint="${REGION}"
  fi
  aws s3api put-bucket-versioning \
    --bucket "${BUCKET}" \
    --versioning-configuration Status=Enabled
  aws s3api put-bucket-encryption \
    --bucket "${BUCKET}" \
    --server-side-encryption-configuration \
    '{"Rules":[{"ApplyServerSideEncryptionByDefault":{"SSEAlgorithm":"AES256"}}]}'
  echo "  Bucket created and configured"
fi
