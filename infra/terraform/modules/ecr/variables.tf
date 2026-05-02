###############################################################################
# infra/terraform/modules/ecr/variables.tf
#
# Input variables for the Sales-Connections ECR module.
#
# Variable groups (in declaration order below):
#   1. Identity / naming                 : name_prefix, environment
#   2. Frontend repo gate                : create_frontend_repo
#   3. Repository configuration          : image_tag_mutability, scan_on_push,
#                                          encryption_type, kms_key_id,
#                                          force_delete
#   4. Lifecycle policy tuning           : lifecycle_policy_keep_count,
#                                          lifecycle_policy_untagged_days
#   5. Repository access policy          : push_role_arn, pull_role_arns
#   6. Tagging                           : tags
#
# Validation rules:
#   - name_prefix: lowercase + digits + hyphens, length-bounded
#   - environment: must be dev / staging / prod
#   - image_tag_mutability: must be MUTABLE or IMMUTABLE
#   - encryption_type: must be AES256 or KMS
#   - lifecycle_policy_keep_count: must be > 0
#   - lifecycle_policy_untagged_days: must be >= 1
#   - push_role_arn: empty OR valid IAM role ARN
#   - pull_role_arns: each entry valid IAM role ARN
#
# Default-value policy (per folder spec):
#   - create_frontend_repo:           true   (most envs deploy container frontend)
#   - image_tag_mutability:           MUTABLE (overridden to IMMUTABLE in prod)
#   - scan_on_push:                   true   (MANDATORY; variable exists for completeness)
#   - encryption_type:                AES256 (acceptable for MVP)
#   - force_delete:                   false  (overridden to true in dev)
#   - lifecycle_policy_keep_count:    30
#   - lifecycle_policy_untagged_days: 7
###############################################################################

###############################################################################
# Identity and naming
###############################################################################

variable "name_prefix" {
  description = "Resource name prefix used in tags, Name labels, and repository names (e.g., 'sales-connections-prod'). Composed by the parent as '$${var.project}-$${var.environment}'. Final ECR repo names will be '$${name_prefix}-backend' and '$${name_prefix}-frontend'."
  type        = string

  validation {
    condition     = length(var.name_prefix) > 0 && length(var.name_prefix) <= 245
    error_message = "name_prefix must be a non-empty string of at most 245 characters (ECR repo name limit is 256, leaving room for '-backend'/'-frontend' suffix)."
  }

  validation {
    condition     = can(regex("^[a-z][a-z0-9-]*[a-z0-9]$", var.name_prefix))
    error_message = "name_prefix must start with a lowercase letter, end with a letter or digit, and contain only lowercase letters, digits, and hyphens (matches ECR repo name rules)."
  }
}

variable "environment" {
  description = "Environment label (one of: dev, staging, prod). Used in Component tag breakdowns and as Environment tag value for cost-explorer drill-down."
  type        = string

  validation {
    condition     = contains(["dev", "staging", "prod"], var.environment)
    error_message = "environment must be one of: dev, staging, prod."
  }
}

###############################################################################
# Frontend repository gate
###############################################################################

variable "create_frontend_repo" {
  description = "Whether to create the frontend ECR repository. Driven by the root composition's local.deploy_frontend_container (i.e., var.frontend_delivery_mode == 'container'). When false, frontend_* outputs return empty strings and no frontend resources are provisioned."
  type        = bool
  default     = true
}

###############################################################################
# Repository configuration
###############################################################################

variable "image_tag_mutability" {
  description = "Image tag mutability policy. 'MUTABLE' allows tag overwrite (typical dev convenience); 'IMMUTABLE' prevents tag overwrite (defense against supply-chain replay attacks; recommended for prod per the folder spec critical constraints)."
  type        = string
  default     = "MUTABLE"

  validation {
    condition     = contains(["MUTABLE", "IMMUTABLE"], var.image_tag_mutability)
    error_message = "image_tag_mutability must be 'MUTABLE' or 'IMMUTABLE'."
  }
}

variable "scan_on_push" {
  description = "Whether the ECR native scanner runs on every push. MANDATORY true per AAP Sec 0.4.6 ('Container image registry with native scanner'). This variable exists for completeness but should never be flipped to false in production environments."
  type        = bool
  default     = true
}

variable "encryption_type" {
  description = "Encryption-at-rest mode for image layers. 'AES256' uses S3-managed keys (acceptable for MVP per folder spec critical constraints). 'KMS' uses a customer-managed CMK identified by var.kms_key_id."
  type        = string
  default     = "AES256"

  validation {
    condition     = contains(["AES256", "KMS"], var.encryption_type)
    error_message = "encryption_type must be 'AES256' or 'KMS'."
  }
}

variable "kms_key_id" {
  description = "KMS key ID or ARN used when encryption_type = 'KMS'. Empty string when encryption_type = 'AES256' (the field is omitted from the AWS API call). Validated by main.tf's encryption_configuration: kms_key is set only when encryption_type == 'KMS' AND kms_key_id != ''."
  type        = string
  default     = ""
}

variable "force_delete" {
  description = "Whether 'terraform destroy' deletes the repository even when images are present. Set true only in dev to enable clean tear-down; false in prod so accidental destroy fails when images exist (defense against operator mistakes)."
  type        = bool
  default     = false
}

###############################################################################
# Lifecycle policy tuning
#
# Two rules per folder spec:
#   - Rule 1 expires UNTAGGED images after lifecycle_policy_untagged_days days.
#   - Rule 2 caps TAGGED images to lifecycle_policy_keep_count newest entries.
###############################################################################

variable "lifecycle_policy_keep_count" {
  description = "Maximum number of tagged images retained in each ECR repository before the oldest are expired. Defaults to 30 per folder spec. Set higher in prod when blue-green deployments require longer rollback windows; set lower in dev to save storage."
  type        = number
  default     = 30

  validation {
    condition     = var.lifecycle_policy_keep_count > 0
    error_message = "lifecycle_policy_keep_count must be greater than 0 (a non-positive value would expire all tagged images on every push)."
  }
}

variable "lifecycle_policy_untagged_days" {
  description = "Days an UNTAGGED image is retained before expiry. Untagged images are typically dangling layers from interrupted pushes or images orphaned by tag overwrites. Defaults to 7 per folder spec."
  type        = number
  default     = 7

  validation {
    condition     = var.lifecycle_policy_untagged_days >= 1
    error_message = "lifecycle_policy_untagged_days must be at least 1 (a value of 0 is rejected by ECR)."
  }
}

###############################################################################
# Repository access policy
#
# When BOTH push_role_arn is empty AND pull_role_arns is empty, no
# aws_ecr_repository_policy resource is created (relying on standard AWS
# account-level IAM access). This allows ECR to be applied during initial
# bootstrap before consumer roles exist.
###############################################################################

variable "push_role_arn" {
  description = "ARN of the IAM role that may push images (typically the GitHub Actions OIDC-federated deploy role assumed by .github/workflows/cd.yml). Empty string disables the push statement in the repository policy. When set together with pull_role_arns, the repository policy enforces both push and pull access via the AAP Sec 0.4.6 two-role separation."
  type        = string
  default     = ""

  validation {
    condition     = var.push_role_arn == "" || can(regex("^arn:aws[a-z\\-]*:iam::[0-9]{12}:role/", var.push_role_arn))
    error_message = "push_role_arn must be empty or a valid IAM role ARN matching 'arn:aws*:iam::ACCOUNT:role/...'."
  }
}

variable "pull_role_arns" {
  description = "List of IAM role ARNs that may pull images (typically [ECS task execution role ARN]). Empty list disables the pull statement in the repository policy. The execution role is what AWS itself uses to pull the image when launching a Fargate task; the application's runtime task role is NOT involved in image pulls."
  type        = list(string)
  default     = []

  validation {
    condition = alltrue([
      for arn in var.pull_role_arns : can(regex("^arn:aws[a-z\\-]*:iam::[0-9]{12}:role/", arn))
    ])
    error_message = "Every entry in pull_role_arns must be a valid IAM role ARN matching 'arn:aws*:iam::ACCOUNT:role/...'."
  }
}

###############################################################################
# Tagging
###############################################################################

variable "tags" {
  description = "Map of tags merged into every resource created by this module. The module layers in 'Component = \"ECR\"' and 'Environment = var.environment' on top of these. The parent composition's provider-level default_tags also apply via AWS provider 5.x default_tags."
  type        = map(string)
  default     = {}
}
