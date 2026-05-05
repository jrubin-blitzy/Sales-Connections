###############################################################################
# infra/terraform/variables.tf
#
# Input variables for the Sales-Connections root composition. Each environment
# (dev/staging/prod) overrides these via its envs/<env>/main.tf composition.
#
# Convention: Variables are grouped into sections by concern. Within each
# section, variables are sorted by importance (most-frequently-overridden
# first). Each variable declares: type, description, default (where safe),
# and validation rules where applicable.
#
# Pinning policy: Variables that affect software/infrastructure versions
# (e.g., db_engine_version) are pinned to specific values per AAP Sec 0.7.7.
###############################################################################

###############################################################################
# Account, region, and environment metadata
###############################################################################

variable "aws_region" {
  description = "AWS region where the stack is deployed (e.g., us-east-1, us-west-2). Each environment may use a different region."
  type        = string
  default     = "us-east-1"

  validation {
    condition     = can(regex("^[a-z]{2}-[a-z]+-[0-9]+$", var.aws_region))
    error_message = "aws_region must be a valid AWS region identifier (e.g., us-east-1, eu-west-2)."
  }
}

variable "environment" {
  description = "Environment name. One of: dev, staging, prod. Drives resource naming, sizing defaults, and observability sensitivity."
  type        = string

  validation {
    condition     = contains(["dev", "staging", "prod"], var.environment)
    error_message = "environment must be one of: dev, staging, prod."
  }
}

variable "repository_identifier" {
  description = "Git repository identifier in OWNER/REPO form (e.g., 'blitzy/sales-connections'). Used as a tag value and in IAM trust policies for OIDC scope-down."
  type        = string
  default     = "blitzy/sales-connections"
}

variable "cost_center" {
  description = "Cost center tag value for billing aggregation (e.g., 'engineering-platform')."
  type        = string
  default     = "engineering-platform"
}

variable "cost_center_owner" {
  description = "Owner email or team identifier for cost center attribution."
  type        = string
  default     = "platform@example.com"
}

variable "additional_tags" {
  description = "Extra tags merged into local.common_tags. Useful for per-environment annotations (e.g., {Tier='internal', SLATier='gold'})."
  type        = map(string)
  default     = {}
}

###############################################################################
# OIDC-federated GitHub Actions trust
###############################################################################

variable "github_actions_deploy_role_arn" {
  description = "ARN of the IAM role that GitHub Actions assumes via OIDC for terraform apply. Empty string disables assume_role (used in local dev). Must be set in CI."
  type        = string
  default     = ""
  sensitive   = false
}

variable "assume_role_external_id" {
  description = "Optional external ID for the assume_role call (defense-in-depth against confused deputy). Empty string disables external_id."
  type        = string
  default     = ""
  sensitive   = true
}

###############################################################################
# Network (VPC, subnets, NAT)
###############################################################################

variable "vpc_cidr" {
  description = "Primary CIDR block for the VPC (e.g., 10.0.0.0/16). Each environment uses a non-overlapping range to allow VPC peering later."
  type        = string
  default     = "10.10.0.0/16"

  validation {
    condition     = can(cidrhost(var.vpc_cidr, 0))
    error_message = "vpc_cidr must be a valid CIDR block."
  }
}

variable "availability_zones" {
  description = "List of availability zones to span. Production should use at least 2 AZs (Multi-AZ); dev may use 1 to save NAT/EIP costs."
  type        = list(string)
  default     = ["us-east-1a", "us-east-1b", "us-east-1c"]

  validation {
    condition     = length(var.availability_zones) >= 1 && length(var.availability_zones) <= 6
    error_message = "availability_zones must contain between 1 and 6 zones."
  }
}

variable "public_subnet_cidrs" {
  description = "CIDR blocks for the public subnets (one per AZ). Used by the ALB. Must be subsets of vpc_cidr and equal in length to availability_zones."
  type        = list(string)
  default     = ["10.10.0.0/24", "10.10.1.0/24", "10.10.2.0/24"]
}

variable "private_subnet_cidrs" {
  description = "CIDR blocks for the private subnets (one per AZ). Used by ECS tasks and RDS. Must be subsets of vpc_cidr and equal in length to availability_zones."
  type        = list(string)
  default     = ["10.10.10.0/24", "10.10.11.0/24", "10.10.12.0/24"]
}

variable "enable_nat_gateway" {
  description = "Whether to provision NAT gateways so private subnets can reach the Internet (required for ECS tasks to pull images from ECR via VPC endpoints OR NAT, and to call Anthropic and Google OAuth APIs). True for all envs in MVP."
  type        = bool
  default     = true
}

variable "single_nat_gateway" {
  description = "If true, use a single NAT gateway across all AZs (cost-optimized for dev). If false, one NAT gateway per AZ (HA-optimized for prod)."
  type        = bool
  default     = false
}

###############################################################################
# Database (RDS PostgreSQL 17.7)
###############################################################################

variable "db_engine_version" {
  description = "PostgreSQL engine version. Pinned to 17.7 LTS per AAP Sec 0.3.1."
  type        = string
  default     = "17.7"

  validation {
    condition     = can(regex("^17\\.", var.db_engine_version))
    error_message = "db_engine_version must be a 17.x version per AAP. Bumping major requires a decision-log entry."
  }
}

variable "db_instance_class" {
  description = "RDS instance class. Defaults to a small Graviton class (db.t4g.medium); production should bump to db.m7g.large or larger."
  type        = string
  default     = "db.t4g.medium"
}

variable "db_allocated_storage" {
  description = "Initial allocated storage in GiB. RDS auto-scales up to db_max_allocated_storage."
  type        = number
  default     = 20

  validation {
    condition     = var.db_allocated_storage >= 20 && var.db_allocated_storage <= 65536
    error_message = "db_allocated_storage must be between 20 and 65536 GiB."
  }
}

variable "db_max_allocated_storage" {
  description = "Maximum auto-scaling storage in GiB. Allows RDS to grow without manual intervention up to this ceiling."
  type        = number
  default     = 200
}

variable "db_multi_az" {
  description = "Whether RDS runs Multi-AZ (synchronous standby in a second AZ). True for prod and staging per AAP Sec 0.4.7; can be false for dev to reduce cost."
  type        = bool
  default     = true
}

variable "db_backup_retention_period" {
  description = "Days of automated backup retention. Range 0-35; production typically 7-30, dev as low as 1."
  type        = number
  default     = 7

  validation {
    condition     = var.db_backup_retention_period >= 0 && var.db_backup_retention_period <= 35
    error_message = "db_backup_retention_period must be between 0 and 35 days."
  }
}

variable "db_deletion_protection" {
  description = "Whether RDS instance deletion is protected. True for prod (default); set false in dev to allow tear-down."
  type        = bool
  default     = true
}

variable "db_performance_insights_enabled" {
  description = "Whether RDS Performance Insights is enabled. Free tier available; useful for query-level diagnostics."
  type        = bool
  default     = true
}

variable "db_database_name" {
  description = "Name of the application database created in RDS at provision time. Application connects to this database."
  type        = string
  default     = "sales_connections"
}

variable "db_master_username" {
  description = "Master username for the RDS instance. Application uses this username to connect (password retrieved from Secrets Manager)."
  type        = string
  default     = "sales_connections_app"

  validation {
    condition     = can(regex("^[a-z][a-z0-9_]{0,62}$", var.db_master_username))
    error_message = "db_master_username must start with a letter and contain only lowercase letters, digits, and underscores (max 63 chars)."
  }
}

###############################################################################
# ECS (Fargate) - backend container compute
###############################################################################

variable "backend_image_tag" {
  description = "Image tag to deploy for the backend ECS task (e.g., 'sha-abc123' or '0.1.0'). The CD pipeline overrides this with the git SHA."
  type        = string
  default     = "latest"

  validation {
    condition     = length(var.backend_image_tag) > 0
    error_message = "backend_image_tag must not be empty."
  }
}

variable "frontend_image_tag" {
  description = "Image tag to deploy for the frontend ECS task (only used when frontend_delivery_mode == 'container')."
  type        = string
  default     = "latest"
}

variable "frontend_delivery_mode" {
  description = "How the frontend SPA is delivered: 'container' (nginx in ECS+ALB) or 'static' (S3+CloudFront). Decision recorded per environment in docs/decision-log.md."
  type        = string
  default     = "container"

  validation {
    condition     = contains(["container", "static"], var.frontend_delivery_mode)
    error_message = "frontend_delivery_mode must be 'container' or 'static'."
  }
}

variable "ecs_task_cpu" {
  description = "CPU units for the backend Fargate task (Fargate valid values: 256, 512, 1024, 2048, 4096, etc.)."
  type        = number
  default     = 512

  validation {
    condition     = contains([256, 512, 1024, 2048, 4096, 8192, 16384], var.ecs_task_cpu)
    error_message = "ecs_task_cpu must be a valid Fargate CPU unit value."
  }
}

variable "ecs_task_memory" {
  description = "Memory (MiB) for the backend Fargate task. Must be valid in combination with ecs_task_cpu (see AWS docs)."
  type        = number
  default     = 1024
}

variable "ecs_service_desired_count" {
  description = "Desired number of running tasks for the backend ECS service. Production typically >= 2 for HA; dev may use 1."
  type        = number
  default     = 2

  validation {
    condition     = var.ecs_service_desired_count >= 1
    error_message = "ecs_service_desired_count must be at least 1."
  }
}

variable "backend_container_port" {
  description = "Container port the backend listens on (matches Gunicorn --bind in backend/Dockerfile). Default 8000."
  type        = number
  default     = 8000
}

variable "frontend_container_port" {
  description = "Container port the frontend nginx listens on (matches frontend/nginx.conf). Default 80."
  type        = number
  default     = 80
}

variable "ecs_enable_execute_command" {
  description = "Whether ECS Exec is enabled on tasks (allows operators to shell into containers for debugging). True for dev/staging, false for prod by default."
  type        = bool
  default     = false
}

###############################################################################
# Backend application configuration (wired as ECS task env vars)
###############################################################################

variable "flask_env" {
  description = "FLASK_ENV value for the backend (drives configuration class selection). Allowed: development, staging, production."
  type        = string
  default     = "production"
}

variable "log_level" {
  description = "LOG_LEVEL for structlog (DEBUG, INFO, WARNING, ERROR, CRITICAL)."
  type        = string
  default     = "INFO"

  validation {
    condition     = contains(["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"], var.log_level)
    error_message = "log_level must be one of: DEBUG, INFO, WARNING, ERROR, CRITICAL."
  }
}

variable "cors_allowed_origins" {
  description = "Comma-separated list of allowed CORS origins for the backend (e.g., 'https://app.sales-connections.example.com')."
  type        = string
  default     = ""
}

variable "default_org_id" {
  description = "UUID of the sole organization served at MVP per AAP Sec 0.7.2 (single-org runtime)."
  type        = string
  default     = "00000000-0000-0000-0000-000000000001"
}

variable "default_new_user_role" {
  description = "Default role assigned to newly upserted OAuth users. Allowed: Admin, Contributor, Viewer."
  type        = string
  default     = "Contributor"

  validation {
    condition     = contains(["Admin", "Contributor", "Viewer"], var.default_new_user_role)
    error_message = "default_new_user_role must be one of: Admin, Contributor, Viewer."
  }
}

variable "anthropic_model" {
  description = "Anthropic Claude model identifier passed to Langchain ChatAnthropic. Pinned per AAP."
  type        = string
  default     = "claude-sonnet-4-5"
}

variable "ai_request_timeout_seconds" {
  description = "Timeout (seconds) for Anthropic API calls. Per AAP Sec 0.7.3, P95 budget is 5s."
  type        = number
  default     = 5

  validation {
    condition     = var.ai_request_timeout_seconds > 0 && var.ai_request_timeout_seconds <= 30
    error_message = "ai_request_timeout_seconds must be between 1 and 30."
  }
}

variable "otlp_exporter_endpoint" {
  description = "OTLP exporter endpoint for distributed tracing. Empty string disables exporter."
  type        = string
  default     = ""
}

###############################################################################
# ALB + TLS + domain
###############################################################################

variable "domain_name" {
  description = "Public domain name where the application is reachable (e.g., 'app.sales-connections.example.com'). Empty string uses the raw ALB DNS."
  type        = string
  default     = ""
}

variable "alternative_domain_names" {
  description = "Additional Subject Alternative Names on the ACM certificate (e.g., for staging and dev domains under the same cert)."
  type        = list(string)
  default     = []
}

variable "acm_certificate_arn" {
  description = "ARN of an existing ACM certificate. Empty string causes the ALB module to provision a new certificate."
  type        = string
  default     = ""
}

variable "alb_access_logs_enabled" {
  description = "Whether to enable ALB access logging to S3 (and forwarding to CloudWatch via subscription)."
  type        = bool
  default     = true
}

###############################################################################
# ECR
###############################################################################

variable "ecr_image_tag_mutability" {
  description = "ECR image tag mutability policy. 'IMMUTABLE' is recommended for prod (prevents tag overwrite); 'MUTABLE' for dev convenience."
  type        = string
  default     = "MUTABLE"

  validation {
    condition     = contains(["MUTABLE", "IMMUTABLE"], var.ecr_image_tag_mutability)
    error_message = "ecr_image_tag_mutability must be 'MUTABLE' or 'IMMUTABLE'."
  }
}

variable "ecr_lifecycle_keep_count" {
  description = "Number of tagged images to retain in each ECR repository. Older images expire under the lifecycle policy."
  type        = number
  default     = 30
}

variable "ecr_lifecycle_untagged_days" {
  description = "Days before untagged images expire. Untagged images are typically dangling layers from failed pushes."
  type        = number
  default     = 7
}

###############################################################################
# Secrets Manager
###############################################################################

variable "secrets_kms_key_id" {
  description = "KMS key ID/ARN for encrypting Secrets Manager entries. Empty string uses the AWS-managed key (acceptable for MVP)."
  type        = string
  default     = ""
}

variable "enable_db_password_rotation" {
  description = "Whether to enable automatic rotation of the RDS master password via a Lambda function. Out of scope for MVP per AAP Sec 0.4.9 default; can be enabled per env."
  type        = bool
  default     = false
}

variable "db_password_rotation_lambda_arn" {
  description = "ARN of the Lambda function performing RDS password rotation. Required only when enable_db_password_rotation is true."
  type        = string
  default     = ""
}

variable "secrets_recovery_window_in_days" {
  description = "Days a deleted Secrets Manager entry is recoverable. Range 7-30; set to 0 to delete immediately (NOT recommended for prod)."
  type        = number
  default     = 30

  validation {
    condition     = var.secrets_recovery_window_in_days == 0 || (var.secrets_recovery_window_in_days >= 7 && var.secrets_recovery_window_in_days <= 30)
    error_message = "secrets_recovery_window_in_days must be 0 (immediate) or between 7 and 30."
  }
}

###############################################################################
# Observability - CloudWatch logs, alarms, dashboard
###############################################################################

variable "log_retention_days" {
  description = "Days of CloudWatch log retention. Production typically 30-90; dev 7-14."
  type        = number
  default     = 30

  validation {
    condition = contains(
      [1, 3, 5, 7, 14, 30, 60, 90, 120, 150, 180, 365, 400, 545, 731, 1827, 3653],
      var.log_retention_days
    )
    error_message = "log_retention_days must be a valid CloudWatch retention value (e.g., 7, 14, 30, 90, 365)."
  }
}

variable "alarm_email_addresses" {
  description = "Email addresses subscribed to the alarm SNS topic. Production may add additional channels."
  type        = list(string)
  default     = []
}

variable "enable_pagerduty_alarms" {
  description = "Whether to fan out alarms to PagerDuty (production-only typically)."
  type        = bool
  default     = false
}

variable "pagerduty_endpoint" {
  description = "PagerDuty integration endpoint (HTTPS URL). Required only when enable_pagerduty_alarms is true."
  type        = string
  default     = ""
  sensitive   = true
}

variable "alarm_threshold_5xx_per_5min" {
  description = "Number of HTTP 5xx responses in a 5-minute window that triggers a high-severity alarm."
  type        = number
  default     = 10
}
