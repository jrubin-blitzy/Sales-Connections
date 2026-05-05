###############################################################################
# infra/terraform/providers.tf
#
# AWS provider configuration for the Sales-Connections root composition.
#
# Authentication strategy:
#   - GitHub Actions: OIDC federation, assuming a deploy role per environment.
#     The deploy role ARN is provided via TF_VAR_github_actions_deploy_role_arn
#     and the assume_role block below activates it. No static keys.
#   - Local development: Falls back to the standard AWS provider chain
#     (env vars, ~/.aws/credentials, instance profile, SSO). Run with
#     `aws sso login` or AWS_PROFILE set as appropriate.
#
# Default tags policy:
#   The provider's default_tags block stamps every taggable resource with
#   project/environment/managed-by metadata. Module-level `tags` arguments
#   merge with these defaults for resource-specific labels (e.g., service
#   names). Per the folder spec: "All resources tag via a mergeable
#   local.common_tags map for cost tracking and environment identification."
#
# Two provider blocks are configured:
#   1. The default provider in var.aws_region (primary region for VPC,
#      RDS, ECS, ALB, ECR, Secrets Manager, CloudWatch).
#   2. An aliased "us_east_1" provider for ACM certificates that back
#      future CloudFront distributions (CloudFront REQUIRES ACM certificates
#      to live in us-east-1). Declared eagerly so the future static-frontend
#      delivery path does not require a breaking change to module wiring.
#
# Provider source/version pins live in versions.tf (hashicorp/aws ~> 5.70).
# State backend (S3 + DynamoDB locking) also lives in versions.tf.
###############################################################################

###############################################################################
# Locals shared across the root composition
#
# common_tags is the SINGLE SOURCE OF TRUTH for project-wide tagging. It is:
#   1. Applied by the AWS provider's default_tags block to every taggable
#      resource (idempotent; AWS provider 5.x merges defaults with
#      resource-level tags correctly).
#   2. Available as `local.common_tags` to main.tf so module inputs that
#      take an explicit tags argument (e.g., for resource types that do
#      NOT honour default_tags, like aws_autoscaling_group, or for cases
#      where module authors want to layer module-specific tags on top)
#      can merge it in.
#
# Tag value policy:
#   - "sales-connections" (Project) is hard-coded because the project name
#     is invariant across environments.
#   - All other values come from variables so each environment composition
#     in envs/<env>/main.tf can override them.
#   - var.additional_tags is merged LAST so per-environment annotations
#     (e.g., {Tier = "internal", SLATier = "gold"}) win over base values.
#   - Tag KEYS use PascalCase per AWS Cost Allocation Tag conventions; tag
#     VALUES use kebab-case where they are identifiers and free-form prose
#     where they are descriptions.
#
# Cost allocation:
#   CostCenter and Owner power the AWS Billing console's cost-explorer
#   breakdowns when this account is shared with other workloads. They are
#   listed here (not in module-specific tags) so the breakdown is uniform
#   across every resource provisioned by this composition.
###############################################################################

locals {
  common_tags = merge(
    {
      Project     = "sales-connections"
      Environment = var.environment
      ManagedBy   = "Terraform"
      Repository  = var.repository_identifier
      Owner       = var.cost_center_owner
      CostCenter  = var.cost_center
    },
    var.additional_tags,
  )
}

###############################################################################
# AWS provider - primary region
#
# This is the default (unaliased) provider used by all modules unless they
# explicitly request an alias. It targets var.aws_region, which is also the
# region where VPC, RDS, ECS, ALB, ECR, Secrets Manager, and CloudWatch
# resources are created.
#
# OIDC-federated assume_role:
#   When TF_VAR_github_actions_deploy_role_arn is set (typically in CI),
#   the dynamic block below activates assume_role with role_arn,
#   session_name, and (optionally) external_id. When the variable is
#   empty (local dev), the dynamic block is omitted and the provider uses
#   the standard AWS credential chain (env vars, ~/.aws/credentials, SSO,
#   instance profile, etc.).
#
#   session_name is set to "TerraformGitHubActions-<environment>" so that
#   every CloudTrail event recorded under this assumed-role session can
#   be attributed to a specific Terraform run via
#   userIdentity.sessionContext.sessionIssuer. This is a critical security
#   forensics capability when investigating "who changed what, when".
#
#   external_id is supplied only when var.assume_role_external_id is
#   non-empty, providing defense-in-depth against the confused deputy
#   problem (see https://docs.aws.amazon.com/IAM/latest/UserGuide/confused-deputy.html).
#
# Default tags:
#   Every taggable AWS resource provisioned by this composition will
#   receive local.common_tags via default_tags. This eliminates the
#   "I forgot to tag this resource" defect class entirely.
###############################################################################

provider "aws" {
  region = var.aws_region

  dynamic "assume_role" {
    for_each = var.github_actions_deploy_role_arn != "" ? [1] : []
    content {
      role_arn     = var.github_actions_deploy_role_arn
      session_name = "TerraformGitHubActions-${var.environment}"
      external_id  = var.assume_role_external_id != "" ? var.assume_role_external_id : null
    }
  }

  default_tags {
    tags = local.common_tags
  }
}

###############################################################################
# AWS provider - us-east-1 (aliased)
#
# CloudFront and ACM certificates that back CloudFront distributions MUST
# live in us-east-1, regardless of where the rest of the stack is deployed.
# This aliased provider exists to support the future
# frontend_delivery_mode = "static" path (S3 + CloudFront), which is
# declared as a variable in variables.tf but not exercised by the MVP
# modules.
#
# Why declare an unused provider?
#   Module compositions that consume an aliased provider via a `providers`
#   meta-argument require the aliased provider to be declared in the ROOT
#   module. Declaring it here costs nothing (Terraform creates an idle
#   provider instance) and unlocks adding CloudFront later WITHOUT a
#   breaking change to existing module wiring.
#
# Same OIDC and default_tags policy as the primary provider:
#   The session_name is suffixed with "-use1" so that CloudTrail events
#   from the us-east-1 region are clearly distinguishable from events in
#   the primary region under the same Terraform run.
###############################################################################

provider "aws" {
  alias  = "us_east_1"
  region = "us-east-1"

  dynamic "assume_role" {
    for_each = var.github_actions_deploy_role_arn != "" ? [1] : []
    content {
      role_arn     = var.github_actions_deploy_role_arn
      session_name = "TerraformGitHubActions-${var.environment}-use1"
      external_id  = var.assume_role_external_id != "" ? var.assume_role_external_id : null
    }
  }

  default_tags {
    tags = local.common_tags
  }
}

###############################################################################
# random and null providers
#
# The hashicorp/random and hashicorp/null providers are declared in
# versions.tf and used by sibling modules:
#   - random_password / random_id   ->  modules/secrets/ for placeholder
#                                       JWT_SIGNING_KEY values when no
#                                       external value source is supplied.
#   - null_resource                 ->  triggers and one-shot local-exec
#                                       hooks (used sparingly).
#
# Neither provider takes configuration arguments, so no provider blocks
# are required here. They appear in versions.tf with pinned versions
# (random ~> 3.6, null ~> 3.2) and Terraform initialises them lazily on
# first reference.
###############################################################################
