# infrastructure/backend.tf
#
# S3 backend for Terraform state.
# The bucket name is resolved at deploy time by deploy.sh via:
#
#   terraform init -backend-config="bucket=momentum-bot-<ACCOUNT>-<REGION>" \
#                  -backend-config="key=momentum-bot/terraform.tfstate" \
#                  -backend-config="region=<REGION>"
#
# Do not hardcode bucket or account id here — deploy.sh injects them.
# No DynamoDB state locking needed (solo operator, tiny state file).

terraform {
  backend "s3" {
    key     = "momentum-bot/terraform.tfstate"
    encrypt = true
    # bucket and region injected by deploy.sh -backend-config flags
  }
}
