###############################################################################
# infra/terraform/modules/database/variables.tf
#
# Input variables for the Sales-Connections RDS PostgreSQL module.
#
# Variable groups (in declaration order below):
#   1. Identity / naming           : name_prefix, environment
#   2. Network placement           : vpc_id, private_subnet_ids,
#                                    allowed_security_group_ids
#   3. Master password source      : master_password_secret_arn
#   4. Engine and sizing           : engine_version, instance_class,
#                                    allocated_storage, max_allocated_storage
#   5. High availability           : multi_az
#   6. Backups                     : backup_retention_period, backup_window
#   7. Maintenance                 : maintenance_window, apply_immediately
#   8. Deletion protection         : deletion_protection, skip_final_snapshot
#   9. Observability               : performance_insights_enabled
#  10. Database identity           : database_name, master_username
#  11. KMS                         : kms_key_id
#  12. Tagging                     : tags
#
# Validation rules:
#   - name_prefix: lowercase + digits + hyphens, length-bounded
#   - environment: dev / staging / prod
#   - vpc_id: must match "^vpc-"
#   - private_subnet_ids: at least 2 entries (Multi-AZ requirement)
#   - allowed_security_group_ids: each must match "^sg-"
#   - master_password_secret_arn: valid Secrets Manager secret ARN
#   - engine_version: must match "^17\." (PostgreSQL 17 LTS pin per AAP)
#   - instance_class: matches "db.<family>.<size>"
#   - allocated_storage: 20-65536 GiB (RDS API limits)
#   - max_allocated_storage: >= 20 GiB (paired sanity check)
#   - backup_retention_period: 0-35 days (RDS API limits)
#   - backup_window: "HH:MM-HH:MM" UTC format
#   - maintenance_window: "ddd:HH:MM-ddd:HH:MM" format
#   - database_name: PostgreSQL identifier rules
#   - master_username: PostgreSQL identifier rules
#
# Default-value policy (per folder spec critical constraints):
#   - engine_version:                  17.7   (PostgreSQL 17 LTS pin)
#   - instance_class:                  db.t4g.medium  (cost-effective Graviton)
#   - allocated_storage:               20     (initial; auto-scales to max)
#   - max_allocated_storage:           200    (storage auto-scaling ceiling)
#   - multi_az:                        true   (Multi-AZ HA per AAP Sec 0.4.7)
#   - backup_retention_period:         7      (1-week retention)
#   - backup_window:                   03:00-04:00 (low-traffic UTC window)
#   - maintenance_window:              Mon:04:00-Mon:05:00 (post-backup)
#   - deletion_protection:             true   (prod default; opt-out for dev)
#   - skip_final_snapshot:             false  (snapshot on destroy by default)
#   - apply_immediately:               false  (waits for maintenance window)
#   - performance_insights_enabled:    true   (free-tier diagnostics)
#   - database_name:                   sales_connections
#   - master_username:                 sales_connections_app
#   - kms_key_id:                      ""     (use AWS-managed alias/aws/rds)
#
# Cross-variable validation note:
#   Terraform 1.7+ does not support referencing other variables inside a
#   variable's validation block; cross-variable conditions (such as
#   "max_allocated_storage >= allocated_storage") are enforced by the
#   AWS RDS API at apply time and/or by lifecycle.precondition blocks on
#   aws_db_instance.main in main.tf.
###############################################################################

###############################################################################
# Identity and naming
###############################################################################

variable "name_prefix" {
  description = "Resource name prefix used in tags, Name labels, and resource identifiers (e.g., 'sales-connections-prod'). Composed by the parent as '$${var.project}-$${var.environment}'. Final RDS instance identifier will be '$${name_prefix}-postgres'."
  type        = string

  validation {
    condition     = length(var.name_prefix) > 0 && length(var.name_prefix) <= 50
    error_message = "name_prefix must be a non-empty string of at most 50 characters (RDS instance identifiers limit to 63 chars total, leaving room for the '-postgres' suffix)."
  }

  validation {
    condition     = can(regex("^[a-z][a-z0-9-]*[a-z0-9]$", var.name_prefix))
    error_message = "name_prefix must start with a lowercase letter, end with a letter or digit, and contain only lowercase letters, digits, and hyphens (matches RDS identifier rules)."
  }
}

variable "environment" {
  description = "Environment label (one of: dev, staging, prod). Used in Name tags and Environment tag for cost-explorer drill-down. Drives default sizing/durability decisions in the parent composition."
  type        = string

  validation {
    condition     = contains(["dev", "staging", "prod"], var.environment)
    error_message = "environment must be one of: dev, staging, prod."
  }
}

###############################################################################
# Network placement
###############################################################################

variable "vpc_id" {
  description = "ID of the VPC hosting the RDS instance. Sourced from module.network.vpc_id in the root composition."
  type        = string

  validation {
    condition     = can(regex("^vpc-[0-9a-f]+$", var.vpc_id))
    error_message = "vpc_id must be a valid VPC ID matching 'vpc-XXXXXXXX'."
  }
}

variable "private_subnet_ids" {
  description = "List of private subnet IDs (at least 2, in different availability zones) for the DB subnet group. RDS provisions the writer in one of these subnets; when var.multi_az = true, the synchronous standby is provisioned in another. Sourced from module.network.private_subnet_ids in the root composition."
  type        = list(string)

  validation {
    condition     = length(var.private_subnet_ids) >= 2
    error_message = "private_subnet_ids must contain at least 2 entries (RDS Multi-AZ requires a standby subnet in a different AZ from the writer)."
  }

  validation {
    condition = alltrue([
      for id in var.private_subnet_ids : can(regex("^subnet-[0-9a-f]+$", id))
    ])
    error_message = "Every entry in private_subnet_ids must be a valid subnet ID matching 'subnet-XXXXXXXX'."
  }
}

variable "allowed_security_group_ids" {
  description = "List of security group IDs whose members are allowed to connect to RDS on port 5432. Typically [<ECS task security group ID>]. Empty list disables ingress (useful during initial bootstrap before ECS exists). Each ID generates one aws_security_group_rule with type=ingress and source_security_group_id=<id>."
  type        = list(string)
  default     = []

  validation {
    condition = alltrue([
      for id in var.allowed_security_group_ids : can(regex("^sg-[0-9a-f]+$", id))
    ])
    error_message = "Every entry in allowed_security_group_ids must be a valid security group ID matching 'sg-XXXXXXXX'."
  }
}

###############################################################################
# Master password source (Secrets Manager)
#
# Per folder spec critical constraints: "Master password from Secrets Manager
# - NEVER hardcoded; read via data.aws_secretsmanager_secret_version".
#
# The data source declared in data.tf reads the value at apply time. The
# value flows into aws_db_instance.main.password but is masked in plan
# output (Terraform automatically marks
# data.aws_secretsmanager_secret_version.secret_string as sensitive).
###############################################################################

variable "master_password_secret_arn" {
  description = "ARN of the AWS Secrets Manager entry holding the RDS master password. Sourced from module.secrets.db_password_arn in the root composition. The data source data.aws_secretsmanager_secret_version reads the value at apply time; lifecycle { ignore_changes = [password] } on aws_db_instance.main allows external rotation without Terraform drift."
  type        = string

  validation {
    condition     = can(regex("^arn:aws[a-z\\-]*:secretsmanager:[a-z0-9\\-]+:[0-9]{12}:secret:", var.master_password_secret_arn))
    error_message = "master_password_secret_arn must be a valid Secrets Manager secret ARN matching 'arn:aws*:secretsmanager:REGION:ACCOUNT:secret:NAME'."
  }
}

###############################################################################
# Engine and sizing
###############################################################################

variable "engine_version" {
  description = "PostgreSQL engine version. Pinned to 17.x per AAP Sec 0.3.1 (PostgreSQL 17 LTS). The locals.tf module derives the parameter group family ('postgres17') from this value. Bumping the major requires updating the validation rule and adding a decision-log entry per the Explainability rule."
  type        = string
  default     = "17.7"

  validation {
    condition     = can(regex("^17\\.", var.engine_version))
    error_message = "engine_version must be a 17.x version per AAP Sec 0.3.1. Major version bumps require explicit operator action (update validation rule + decision-log entry)."
  }
}

variable "instance_class" {
  description = "RDS instance class controlling CPU/memory/network capacity. Defaults to db.t4g.medium (Graviton, 2 vCPU, 4 GiB) - cost-effective for MVP scale (10K records per org). Production at heavier load should bump to db.m7g.large or larger. Format: 'db.<family>.<size>'."
  type        = string
  default     = "db.t4g.medium"

  validation {
    condition     = can(regex("^db\\.[a-z0-9]+\\.[a-z0-9]+$", var.instance_class))
    error_message = "instance_class must match the format 'db.<family>.<size>' (e.g., 'db.t4g.medium', 'db.m7g.large')."
  }
}

variable "allocated_storage" {
  description = "Initial allocated storage in GiB. RDS auto-scales up to var.max_allocated_storage when storage utilization exceeds 90%. Range 20-65536 (AWS API limits). Default 20 is appropriate for MVP."
  type        = number
  default     = 20

  validation {
    condition     = var.allocated_storage >= 20 && var.allocated_storage <= 65536
    error_message = "allocated_storage must be between 20 and 65536 GiB (AWS API limits)."
  }
}

variable "max_allocated_storage" {
  description = "Maximum auto-scaling storage in GiB. Allows RDS to grow without manual intervention up to this ceiling. Must be >= var.allocated_storage (paired sanity check enforced via lifecycle.precondition in main.tf, since Terraform 1.7 does not support cross-variable validation). Default 200 covers projected MVP growth (10K records per org)."
  type        = number
  default     = 200

  validation {
    condition     = var.max_allocated_storage >= 20
    error_message = "max_allocated_storage must be at least 20 GiB (paired with allocated_storage minimum)."
  }
}

###############################################################################
# High availability
###############################################################################

variable "multi_az" {
  description = "Whether to provision the RDS instance in Multi-AZ mode (synchronous standby in a second AZ). Per AAP Sec 0.4.7 and folder spec critical constraints: prod MUST set this to true. Dev environments may set false to reduce cost."
  type        = bool
  default     = true
}

###############################################################################
# Backups
###############################################################################

variable "backup_retention_period" {
  description = "Days of automated backup retention. Range 0-35 (AWS API limits). 0 disables automated backups (NOT recommended for prod). Default 7 (1-week retention) provides a reasonable recovery window."
  type        = number
  default     = 7

  validation {
    condition     = var.backup_retention_period >= 0 && var.backup_retention_period <= 35
    error_message = "backup_retention_period must be between 0 and 35 days (AWS API limits). Production should be at least 7."
  }
}

variable "backup_window" {
  description = "Daily backup window in UTC, format 'HH:MM-HH:MM'. Should target a low-traffic period for the application. Default '03:00-04:00' UTC (off-hours for US business traffic)."
  type        = string
  default     = "03:00-04:00"

  validation {
    condition     = can(regex("^[0-2][0-9]:[0-5][0-9]-[0-2][0-9]:[0-5][0-9]$", var.backup_window))
    error_message = "backup_window must match 'HH:MM-HH:MM' (24-hour UTC, e.g., '03:00-04:00')."
  }
}

###############################################################################
# Maintenance
###############################################################################

variable "maintenance_window" {
  description = "Weekly maintenance window in UTC, format 'ddd:HH:MM-ddd:HH:MM' (e.g., 'Mon:04:00-Mon:05:00'). Should follow the backup window so backups complete first. Default 'Mon:04:00-Mon:05:00' (Monday post-backup hour, low-traffic in US)."
  type        = string
  default     = "Mon:04:00-Mon:05:00"

  validation {
    condition     = can(regex("^(Mon|Tue|Wed|Thu|Fri|Sat|Sun):[0-2][0-9]:[0-5][0-9]-(Mon|Tue|Wed|Thu|Fri|Sat|Sun):[0-2][0-9]:[0-5][0-9]$", var.maintenance_window))
    error_message = "maintenance_window must match 'ddd:HH:MM-ddd:HH:MM' (3-letter day abbreviation, 24-hour UTC, e.g., 'Mon:04:00-Mon:05:00')."
  }
}

variable "apply_immediately" {
  description = "Whether modifications are applied immediately (vs. waiting for the maintenance window). Default false (production-safe: changes wait for the maintenance window, allowing review). Set true in dev for fast iteration."
  type        = bool
  default     = false
}

###############################################################################
# Deletion protection
###############################################################################

variable "deletion_protection" {
  description = "Whether RDS instance deletion is protected. Per folder spec critical constraints: prod REQUIRED true. When true, the instance cannot be deleted via 'terraform destroy' or aws-cli without first disabling protection. Default true (production-safe)."
  type        = bool
  default     = true
}

variable "skip_final_snapshot" {
  description = "Whether to skip the final snapshot when the instance is destroyed. Per folder spec critical constraints: prod REQUIRED false. When false, RDS takes a final snapshot named '$${name_prefix}-postgres-final-<timestamp>' before deletion. Set true in dev to enable clean tear-down."
  type        = bool
  default     = false
}

###############################################################################
# Observability
###############################################################################

variable "performance_insights_enabled" {
  description = "Whether to enable RDS Performance Insights. Free tier includes 7-day retention (used here). Default true per folder spec critical constraints: 'Performance Insights - Enabled by default (free tier)'. Provides query-level metrics consumed by the CloudWatch dashboard."
  type        = bool
  default     = true
}

###############################################################################
# Database identity
###############################################################################

variable "database_name" {
  description = "Name of the application database created at RDS provision time. Backend connects to this database using the master credentials. Default 'sales_connections' matches AAP Sec 0.5.2 implementation directives."
  type        = string
  default     = "sales_connections"

  validation {
    condition     = can(regex("^[a-z_][a-z0-9_]{0,62}$", var.database_name))
    error_message = "database_name must start with a lowercase letter or underscore and contain only lowercase letters, digits, and underscores (PostgreSQL identifier rules), max 63 chars."
  }
}

variable "master_username" {
  description = "Master username for the RDS instance. Application uses this username to connect (password retrieved from Secrets Manager). Default 'sales_connections_app' matches AAP Sec 0.5.2 implementation directives. Note: 'rdsadmin' and certain other reserved names are rejected by RDS."
  type        = string
  default     = "sales_connections_app"

  validation {
    condition     = can(regex("^[a-z][a-z0-9_]{0,62}$", var.master_username))
    error_message = "master_username must start with a lowercase letter and contain only lowercase letters, digits, and underscores (PostgreSQL identifier rules), max 63 chars."
  }
}

###############################################################################
# KMS encryption
#
# Per folder spec critical constraints: storage_encrypted = true is hardcoded
# in main.tf (mandatory). The kms_key_id variable selects the key:
#   - Empty string (default): AWS-managed key (alias/aws/rds), acceptable for MVP.
#   - Non-empty: customer-managed CMK identified by ARN/ID/alias.
#
# locals.tf coerces empty string to null because the AWS provider rejects an
# empty-string kms_key_id directly on the resource.
###############################################################################

variable "kms_key_id" {
  description = "KMS key ID, ARN, or alias for encrypting RDS storage at rest. Empty string (default) uses the AWS-managed key 'alias/aws/rds' (acceptable for MVP per folder spec critical constraints). Set to a customer-managed CMK ARN to override. The locals.tf module coerces empty to null inside the resource argument."
  type        = string
  default     = ""
}

###############################################################################
# Tagging
###############################################################################

variable "tags" {
  description = "Map of tags merged into every resource created by this module. The module layers in 'Component = \"Database\"' and 'Environment = var.environment' on top of these (in locals.tf). Each resource additionally layers in a 'Name' tag at the resource site. The parent composition's provider-level default_tags also apply via AWS provider 5.x default_tags."
  type        = map(string)
  default     = {}
}
