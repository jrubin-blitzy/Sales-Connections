###############################################################################
# infra/terraform/envs/prod/main.tf
#
# Production environment composition for the Sales-Connections platform.
# Wraps the root composition at ../../ (infra/terraform/) as a stack module
# and applies production-grade variable values to it.
#
# Production-grade settings applied here (NON-NEGOTIABLE per AAP):
#   - Multi-AZ RDS (db_multi_az = true)            per AAP Sec 0.4.7
#   - Multi-AZ NAT gateway (single_nat_gateway = false)
#   - IMMUTABLE ECR tags (ecr_image_tag_mutability = "IMMUTABLE")
#   - ECS Exec disabled (ecs_enable_execute_command = false)
#   - Deletion protection on (db_deletion_protection = true)
#   - PagerDuty alarms enabled (enable_pagerduty_alarms = true)
#   - 3 Fargate tasks for HA, 1024 CPU / 2048 MiB memory each
#   - 30-day RDS backup retention; 90-day CloudWatch log retention
#   - 30-day Secrets Manager recovery window
#
# State backend: S3 bucket sales-connections-tfstate-prod plus DynamoDB
# lock table sales-connections-tfstate-locks. Bootstrapped once per AWS
# account per the runbook in docs/operations.md.
#
# CRITICAL: terraform apply REQUIRES manual approval per AAP Sec 0.5.2 -
# enforced by .github/workflows/cd.yml `environment: prod` gate. Changes
# MUST be reviewed by an Admin / SRE per .github/CODEOWNERS. var.domain_name
# and var.acm_certificate_arn MUST be supplied via TF_VAR_* env vars before
# the first apply; defaults are empty to permit `terraform validate` only.
#
# Provider source/version pins are inherited from infra/terraform/versions.tf
# via the module call. local.common_tags is applied by default_tags from
# infra/terraform/providers.tf. Decision rationale for every non-trivial
# choice is recorded in docs/decision-log.md per AAP Sec 0.7.5.
###############################################################################

terraform {
  required_version = ">= 1.7.0, < 2.0.0"

  ##############################################################################
  # State backend - hardcoded (not partial) because each environment has a
  # fixed, known bucket and lock table. The bucket is bootstrapped once per
  # AWS account by the script in docs/operations.md.
  ##############################################################################
  backend "s3" {
    bucket         = "sales-connections-tfstate-prod"
    key            = "sales-connections/prod/terraform.tfstate"
    region         = "us-east-1"
    dynamodb_table = "sales-connections-tfstate-locks"
    encrypt        = true
  }
}

###############################################################################
# Input variables - CI / operator injection points
#
# Declared locally so the GitHub Actions CD pipeline (and operators on the
# local CLI) can pass values via TF_VAR_* environment variables WITHOUT
# modifying this file. Defaults are empty so `terraform validate` and
# `terraform plan` can run for syntax checking without real values.
###############################################################################

variable "backend_image_tag" {
  description = "Backend container image tag to deploy (CI sets this to the git SHA after manual approval; defaults to 'latest' for bootstrap)."
  type        = string
  default     = "latest"
}

variable "frontend_image_tag" {
  description = "Frontend container image tag to deploy (CI sets this to the git SHA after manual approval; defaults to 'latest' for bootstrap)."
  type        = string
  default     = "latest"
}

variable "github_actions_deploy_role_arn" {
  description = "ARN of the IAM role assumed by GitHub Actions via OIDC for terraform apply. REQUIRED in prod; passed via TF_VAR_github_actions_deploy_role_arn from the CD workflow."
  type        = string
  default     = ""
}

variable "assume_role_external_id" {
  description = "External ID for the OIDC assume_role call (defense-in-depth against confused deputy). Recommended in prod; passed via TF_VAR_assume_role_external_id."
  type        = string
  default     = ""
  sensitive   = true
}

variable "acm_certificate_arn" {
  description = "ARN of an existing ACM certificate covering var.domain_name. REQUIRED in prod; the apex domain certificate is provisioned out-of-band (no auto-issuance for the production apex)."
  type        = string
  default     = ""
}

variable "domain_name" {
  description = "Public domain for the production application (e.g., 'app.example.com'). REQUIRED in prod; must match a SAN on var.acm_certificate_arn."
  type        = string
  default     = ""
}

variable "alternative_domain_names" {
  description = "Additional Subject Alternative Names on the ACM certificate (e.g., apex + www). Optional; defaults to empty list."
  type        = list(string)
  default     = []
}

variable "alarm_email_addresses" {
  description = "Email addresses subscribed to the SNS alarm topic. At least one mailbox or PagerDuty endpoint is REQUIRED for prod observability per AAP Sec 0.7.5."
  type        = list(string)
  default     = []
}

variable "pagerduty_endpoint" {
  description = "PagerDuty integration endpoint (HTTPS URL). REQUIRED in prod; passed via TF_VAR_pagerduty_endpoint to keep the value out of source control."
  type        = string
  default     = ""
  sensitive   = true
}

variable "secrets_kms_key_id" {
  description = "Customer-managed KMS key for Secrets Manager. Empty string uses the AWS-managed alias/aws/secretsmanager (acceptable for MVP per AAP Sec 0.4.9; a CMK is recommended for prod)."
  type        = string
  default     = ""
}

###############################################################################
# Stack module - the prod composition body
#
# Calls the root composition at ../../ and applies production-grade values.
# Every attribute here resolves to a `variable` block declared in
# infra/terraform/variables.tf and validated at plan time.
###############################################################################

module "stack" {
  source = "../../"

  # Environment metadata.
  aws_region            = "us-east-1"
  environment           = "prod"
  repository_identifier = "blitzy/sales-connections"
  cost_center           = "engineering-platform"
  cost_center_owner     = "platform@example.com"

  # OIDC trust - long-lived AWS keys forbidden per AAP Sec 0.7.4.
  github_actions_deploy_role_arn = var.github_actions_deploy_role_arn
  assume_role_external_id        = var.assume_role_external_id

  # Network: 3 AZs, multi-AZ NAT for HA. CIDR 10.30.0.0/16 is non-overlapping
  # with dev (10.10.0.0/16) and staging (10.20.0.0/16) for future VPC peering.
  vpc_cidr             = "10.30.0.0/16"
  availability_zones   = ["us-east-1a", "us-east-1b", "us-east-1c"]
  public_subnet_cidrs  = ["10.30.0.0/24", "10.30.1.0/24", "10.30.2.0/24"]
  private_subnet_cidrs = ["10.30.10.0/24", "10.30.11.0/24", "10.30.12.0/24"]
  enable_nat_gateway   = true
  single_nat_gateway   = false

  # Database: db.m7g.large Graviton, Multi-AZ MANDATORY per AAP Sec 0.4.7.
  db_engine_version               = "17.7"
  db_instance_class               = "db.m7g.large"
  db_allocated_storage            = 100
  db_max_allocated_storage        = 500
  db_multi_az                     = true
  db_backup_retention_period      = 30
  db_deletion_protection          = true
  db_performance_insights_enabled = true
  db_database_name                = "sales_connections"
  db_master_username              = "sales_connections_app"

  # ECR: IMMUTABLE tags MANDATORY in prod for traceability and rollback safety.
  ecr_image_tag_mutability    = "IMMUTABLE"
  ecr_lifecycle_keep_count    = 50
  ecr_lifecycle_untagged_days = 7

  # ECS (Fargate): 1024 CPU / 2048 MiB; 3 tasks for HA. ECS Exec disabled.
  backend_image_tag          = var.backend_image_tag
  frontend_image_tag         = var.frontend_image_tag
  frontend_delivery_mode     = "container"
  ecs_task_cpu               = 1024
  ecs_task_memory            = 2048
  ecs_service_desired_count  = 3
  backend_container_port     = 8000
  frontend_container_port    = 80
  ecs_enable_execute_command = false

  # Backend application configuration - non-secret env vars only.
  flask_env                  = "production"
  log_level                  = "INFO"
  cors_allowed_origins       = ""
  default_org_id             = "00000000-0000-0000-0000-000000000001"
  default_new_user_role      = "Contributor"
  anthropic_model            = "claude-sonnet-4-5"
  ai_request_timeout_seconds = 5
  otlp_exporter_endpoint     = ""

  # ALB / TLS - domain and ACM cert REQUIRED in prod. Access logs ON.
  domain_name              = var.domain_name
  acm_certificate_arn      = var.acm_certificate_arn
  alternative_domain_names = var.alternative_domain_names
  alb_access_logs_enabled  = true

  # Secrets Manager - 30-day recovery (max). Rotation deferred per AAP Sec 0.4.9.
  secrets_kms_key_id              = var.secrets_kms_key_id
  enable_db_password_rotation     = false
  db_password_rotation_lambda_arn = ""
  secrets_recovery_window_in_days = 30

  # Observability - 90-day retention; PagerDuty MANDATORY per AAP Sec 0.7.5.
  log_retention_days           = 90
  alarm_email_addresses        = var.alarm_email_addresses
  enable_pagerduty_alarms      = true
  pagerduty_endpoint           = var.pagerduty_endpoint
  alarm_threshold_5xx_per_5min = 10

  additional_tags = {
    Tier    = "production"
    SLATier = "production"
  }
}

###############################################################################
# Outputs - re-export selected values from module.stack
#
# Operators run `terraform output -raw <name>` to retrieve identifiers used
# by the CD pipeline (ECR URLs, ECS service identifiers) and incident-
# response runbooks (ALB DNS, dashboard URL). Per AAP Sec 0.7.4 NO secret
# values cross this boundary - only ARNs, IDs, and DNS names.
###############################################################################

output "vpc_id" {
  description = "ID of the VPC hosting the Sales-Connections production stack. Surfaced for diagnostic reference."
  value       = module.stack.vpc_id
}

output "private_subnet_ids" {
  description = "List of private subnet IDs across the three production AZs. Hosts ECS Fargate tasks and the RDS DB subnet group."
  value       = module.stack.private_subnet_ids
}

output "public_subnet_ids" {
  description = "List of public subnet IDs across the three production AZs. Hosts the ALB."
  value       = module.stack.public_subnet_ids
}

output "rds_endpoint" {
  description = "RDS PostgreSQL writer endpoint in 'host:port' form. The application constructs the SQLAlchemy DSN from this plus credentials sourced from Secrets Manager at task launch."
  value       = module.stack.rds_endpoint
}

output "rds_database_name" {
  description = "Name of the production application database (defaults to 'sales_connections'). Used by SQLAlchemy DSN composition."
  value       = module.stack.rds_database_name
}

output "ecr_backend_repository_url" {
  description = "URL of the production backend ECR repository. Consumed by .github/workflows/cd.yml for image push after manual approval."
  value       = module.stack.ecr_backend_repository_url
}

output "ecr_frontend_repository_url" {
  description = "URL of the production frontend ECR repository. Consumed by .github/workflows/cd.yml for nginx image push when frontend_delivery_mode == 'container'."
  value       = module.stack.ecr_frontend_repository_url
}

output "ecs_cluster_name" {
  description = "ECS cluster name hosting the production Fargate service. Used by `aws ecs describe-services` and by CD pipeline service-update commands."
  value       = module.stack.ecs_cluster_name
}

output "ecs_service_name" {
  description = "Name of the production backend ECS service. Used by the CD pipeline for `aws ecs update-service` to roll out new task definitions after manual approval."
  value       = module.stack.ecs_service_name
}

output "alb_dns_name" {
  description = "DNS name of the production ALB. CNAME this in Route53 to var.domain_name (typically done out-of-band on first apply)."
  value       = module.stack.alb_dns_name
}

output "application_url" {
  description = "Public URL where the production application can be reached over HTTPS. Uses var.domain_name when set; falls back to the raw ALB DNS otherwise."
  value       = module.stack.application_url
}

output "cloudwatch_dashboard_url" {
  description = "Direct URL to the production CloudWatch dashboard (region-aware). Operators paste this URL into incident channels and runbooks during incident response."
  value       = module.stack.cloudwatch_dashboard_url
}
