###############################################################################
# infra/terraform/versions.tf
#
# Terraform binary version + provider source/version pins for the
# Sales-Connections root composition.
#
# Pinning policy (AAP Sec 0.7.7):
#   - Terraform binary: >= 1.7.0, < 2.0.0
#   - AWS provider:     ~> 5.70 (any 5.70+ patch / 5.x minor)
#   - random:           ~> 3.6
#   - null:             ~> 3.2
#
# Backend:
#   S3 with DynamoDB locking. The block here is PARTIAL - bucket, key,
#   region, and dynamodb_table are supplied at init time via:
#       terraform init -backend-config=envs/<env>/<env>.s3.tfbackend
#
#   The partial-config approach lets a single root composition target
#   different state files per environment without duplicating Terraform
#   files. Bootstrap of the S3 bucket and DynamoDB table is performed
#   once per AWS account via the script in docs/operations.md.
###############################################################################

terraform {
  required_version = ">= 1.7.0, < 2.0.0"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.70"
    }

    random = {
      source  = "hashicorp/random"
      version = "~> 3.6"
    }

    null = {
      source  = "hashicorp/null"
      version = "~> 3.2"
    }
  }

  #############################################################################
  # State backend: S3 + DynamoDB locking (partial config)
  #
  # Concrete backend configuration values (bucket, key, region,
  # dynamodb_table) are supplied at init time. Example for the dev env:
  #
  #     terraform init \
  #       -backend-config="bucket=sales-connections-tfstate-dev" \
  #       -backend-config="key=sales-connections/dev/terraform.tfstate" \
  #       -backend-config="region=us-east-1" \
  #       -backend-config="dynamodb_table=sales-connections-tfstate-locks" \
  #       -backend-config="encrypt=true"
  #
  # OR via a backend config file:
  #
  #     terraform init -backend-config=envs/dev/dev.s3.tfbackend
  #
  # See docs/operations.md for the bootstrap script that creates the
  # state bucket and the DynamoDB lock table once per AWS account.
  #
  # Locking strategy:
  #   - DynamoDB-based locking is the canonical, portable pattern that
  #     works across all Terraform 1.7+ binaries.
  #   - `use_lockfile = false` is set explicitly to document the intent:
  #     we are NOT using the native S3 lock-file feature (which would
  #     require Terraform 1.10+ to be guaranteed across the team and
  #     would orphan locks if any operator runs an older binary).
  #   - When the team migrates to Terraform 1.10+ exclusively and
  #     decides to retire DynamoDB locking, flip this flag to true and
  #     run `terraform init -reconfigure`. Record the migration in
  #     docs/decision-log.md.
  #############################################################################
  backend "s3" {
    encrypt      = true
    use_lockfile = false
    # bucket         = supplied at init time
    # key            = supplied at init time
    # region         = supplied at init time
    # dynamodb_table = supplied at init time
  }
}

###############################################################################
# Contract maintained by this file:
#
# 1. The Terraform binary used to plan/apply this composition MUST be
#    >= 1.7.0 and < 2.0.0. Older binaries reject the configuration; newer
#    major versions require an explicit decision-log entry to bump.
#
# 2. The AWS provider used MUST be 5.70 or later within the 5.x major.
#    Renovate-style updates are accepted via Dependabot (subject to a
#    decision-log entry for any breaking change). Provider 5.x is
#    required for native support of default_tags, RDS PostgreSQL 17.x
#    engine versions, and modern ECS Fargate parameters.
#
# 3. The `random` and `null` providers are utility providers used by
#    sibling modules (e.g., random_password for placeholder secret values
#    when no real value is supplied; null_resource for triggers).
#
# 4. The state backend is S3 with DynamoDB locking. The bucket, key,
#    region, and lock table are supplied at init time via partial backend
#    configuration. State encryption at rest is enabled.
###############################################################################
