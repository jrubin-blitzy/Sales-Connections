###############################################################################
# infra/terraform/modules/secrets/variables.tf
#
# Input variables for the Sales-Connections secrets module.
#
# Variable groups (in declaration order below):
#   1. Identity / naming               : name_prefix, environment
#   2. Reader principals (required)    : reader_principal_arns
#   3. KMS / recovery / lifecycle      : kms_key_id, recovery_window_in_days
#   4. Value population strategy       : write_placeholder_versions,
#                                        generate_jwt_key, generate_db_password
#   5. Optional rotation               : enable_db_password_rotation,
#                                        rotation_lambda_arn
#   6. Tagging                         : tags
#
# Validation rules:
#   - name_prefix: lowercase + digits + hyphens, length-bounded
#   - environment: dev / staging / prod
#   - reader_principal_arns: non-empty, each entry a valid IAM role ARN
#   - recovery_window_in_days: 0 (immediate) or 7-30
#   - rotation_lambda_arn: empty OR valid Lambda ARN
#
# Default-value policy (per folder spec critical constraints):
#   - kms_key_id:                  ""    (AWS-managed key; CMK acceptable for MVP)
#   - recovery_window_in_days:     30    (prod default; allows accidental-delete recovery)
#   - write_placeholder_versions:  true  (dev default; flipped to false in prod env composition)
#   - generate_jwt_key:            true  (dev default; flipped to false in prod when external population required)
#   - generate_db_password:        true  (dev default; flipped to false in prod with rotation)
#   - enable_db_password_rotation: false (out of scope for MVP per AAP Sec 0.4.9)
#   - rotation_lambda_arn:         ""    (only required when rotation enabled)
###############################################################################

###############################################################################
# Identity and naming
###############################################################################

variable "name_prefix" {
  description = "Resource name prefix used in tags, Name labels, and the path-based hierarchy of secret names (e.g., 'sales-connections-prod'). Composed by the parent as '$${var.project}-$${var.environment}'. Final secret names follow the pattern '$${name_prefix}/<secret_name>' (slash separator for the Secrets Manager console's tree view)."
  type        = string

  validation {
    condition     = length(var.name_prefix) > 0 && length(var.name_prefix) <= 240
    error_message = "name_prefix must be a non-empty string of at most 240 characters (Secrets Manager name limit is 512, leaving room for the slash plus longest secret_name suffix '/google_oauth_client_secret' = 27 chars)."
  }

  validation {
    condition     = can(regex("^[a-z][a-z0-9-]*[a-z0-9]$", var.name_prefix))
    error_message = "name_prefix must start with a lowercase letter, end with a letter or digit, and contain only lowercase letters, digits, and hyphens (path-safe and DNS-friendly)."
  }
}

variable "environment" {
  description = "Environment label (one of: dev, staging, prod). Used as the Environment tag value for cost-explorer drill-down and as part of the Component-tag breakdown."
  type        = string

  validation {
    condition     = contains(["dev", "staging", "prod"], var.environment)
    error_message = "environment must be one of: dev, staging, prod."
  }
}

###############################################################################
# Reader principals
#
# Per folder spec: "Resource policies grant read to ECS task role ONLY -
# Least privilege enforcement; var.reader_principal_arns is the sole list
# of allowed principals."
#
# Typically [module.ecs.task_role_arn] in the root composition. The list
# allows callers to add additional read consumers (e.g., a future
# diagnostic Lambda) without editing the module.
###############################################################################

variable "reader_principal_arns" {
  description = "List of IAM role ARNs granted secretsmanager:GetSecretValue on every secret managed by this module. Typically a singleton [<ECS task role ARN>]. The list MUST be non-empty; an empty list would generate an invalid IAM policy that AWS rejects. Per AAP Sec 0.7.4 least-privilege invariant: this is the ONLY mechanism by which read access is granted."
  type        = list(string)

  validation {
    condition     = length(var.reader_principal_arns) > 0
    error_message = "reader_principal_arns must contain at least one entry. Per the least-privilege invariant, the ECS task role ARN is the canonical reader; supply [module.ecs.task_role_arn] from the root composition."
  }

  validation {
    condition = alltrue([
      for arn in var.reader_principal_arns :
      can(regex("^arn:aws[a-z\\-]*:iam::[0-9]{12}:role/", arn))
    ])
    error_message = "Every entry in reader_principal_arns must be a valid IAM role ARN matching 'arn:aws*:iam::ACCOUNT:role/...'."
  }
}

###############################################################################
# KMS encryption / recovery
###############################################################################

variable "kms_key_id" {
  description = "KMS key ID, ARN, or alias for encrypting Secrets Manager entries at rest. Empty string (default) uses the AWS-managed key 'alias/aws/secretsmanager' (acceptable for MVP per AAP Sec 0.4.9). Set to a customer-managed CMK ARN to override. The module coerces empty to null inside locals.tf because the AWS provider rejects an empty-string kms_key_id."
  type        = string
  default     = ""
}

variable "recovery_window_in_days" {
  description = "Number of days a deleted Secrets Manager entry is recoverable. 0 = immediate deletion (dev tear-down only); 7-30 = standard recovery window (prod default 30). Per folder spec critical constraints: 'recovery_window_in_days = 30 for prod (default); allows accidental-delete recovery'."
  type        = number
  default     = 30

  validation {
    condition     = var.recovery_window_in_days == 0 || (var.recovery_window_in_days >= 7 && var.recovery_window_in_days <= 30)
    error_message = "recovery_window_in_days must be 0 (immediate deletion; dev only) or between 7 and 30 (AWS-imposed range)."
  }
}

###############################################################################
# Value population strategy
#
# Per folder spec critical constraints:
#   "Two-secret-population pattern:
#    - Dev/Staging: Terraform generates placeholder values via
#      random_password or writes literal 'placeholder-replace-via-cli'
#    - Prod: External population via operator CLI; Terraform tracks the
#      secret resource but ignores the value"
###############################################################################

variable "write_placeholder_versions" {
  description = "Whether Terraform writes a literal 'placeholder-replace-via-cli' string as the initial secret_string on the four Secrets Manager entries. Default true (dev convenience: secrets exist with a recognizable placeholder until operators populate the real values via 'aws secretsmanager put-secret-value'). Set false in production envs/prod/main.tf so Terraform creates the secret entries without writing any value (operators populate externally from a secure workstation). The lifecycle { ignore_changes = [secret_string] } block on each version resource prevents subsequent applies from clobbering operator-populated values regardless of this variable."
  type        = bool
  default     = true
}

variable "generate_jwt_key" {
  description = "Whether Terraform uses random_password to generate the JWT_SIGNING_KEY value (64 chars, alphanumeric, URL-safe alphabet) and writes it as the initial secret_version. Default true: Terraform produces a stable value from random state. Set false to skip generation (operators populate externally; useful when an external KMS-backed key derivation is in place)."
  type        = bool
  default     = true
}

variable "generate_db_password" {
  description = "Whether Terraform uses random_password to generate the DB_PASSWORD value (32 chars, PostgreSQL-safe special characters) and writes it as the initial secret_version. Default true: typical for dev where the password lifecycle is simple. Set false in prod with rotation enabled (the rotation Lambda owns the value lifecycle and Terraform must not seed it)."
  type        = bool
  default     = true
}

###############################################################################
# Optional rotation (DB password only)
#
# Per AAP Sec 0.4.9: "automatic rotation Lambda built in this module -
# enable_db_password_rotation is supported as a hook (consumes external
# rotation_lambda_arn), but the rotation Lambda itself is post-MVP".
###############################################################################

variable "enable_db_password_rotation" {
  description = "Whether to enable automatic rotation of the DB master password via aws_secretsmanager_secret_rotation. Default false (out of scope for MVP per AAP Sec 0.4.9). When true, var.rotation_lambda_arn must be a valid Lambda ARN; the rotation cadence is fixed at 30 days. The rotation Lambda itself is provisioned OUTSIDE this module."
  type        = bool
  default     = false
}

variable "rotation_lambda_arn" {
  description = "ARN of the externally-provisioned Lambda function that rotates the DB master password. Required when var.enable_db_password_rotation is true; ignored otherwise. The Lambda must implement the AWS Secrets Manager rotation lifecycle (createSecret -> setSecret -> testSecret -> finishSecret) and have lambda:InvokeFunction granted to the Secrets Manager service principal."
  type        = string
  default     = ""

  validation {
    condition     = var.rotation_lambda_arn == "" || can(regex("^arn:aws[a-z\\-]*:lambda:[a-z0-9\\-]+:[0-9]{12}:function:", var.rotation_lambda_arn))
    error_message = "rotation_lambda_arn must be empty or a valid Lambda function ARN matching 'arn:aws*:lambda:REGION:ACCOUNT:function:NAME'."
  }
}

###############################################################################
# Tagging
###############################################################################

variable "tags" {
  description = "Map of tags merged into every resource created by this module. The module layers in 'Component = \"Secrets\"' and 'Environment = var.environment' on top of these (in locals.tf). Per-secret resources additionally layer in 'SecretType' (ApiKey / ClientSecret / SigningKey / DatabasePassword) and 'Service' labels. The parent composition's provider-level default_tags also apply via AWS provider 5.x default_tags."
  type        = map(string)
  default     = {}
}
