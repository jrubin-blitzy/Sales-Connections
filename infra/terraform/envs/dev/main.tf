###############################################################################
# infra/terraform/envs/dev/main.tf
#
# Development environment composition for the Sales-Connections platform.
# Wraps the root composition at ../../ (infra/terraform/) as a stack module
# and applies dev-grade variable values to it.
#
# Dev is the COST-OPTIMIZED iteration environment. It deliberately deviates
# from prod-grade settings to reduce hourly spend and accelerate developer
# feedback loops. The trade-offs below are documented inline so anyone
# copying this file to envs/staging or envs/prod must override them:
#
#   Cost-optimized settings (NOT production-safe):
#     - Single NAT gateway              (single_nat_gateway = true)
#     - Single-AZ RDS                   (db_multi_az = false)
#     - MUTABLE ECR tags                (ecr_image_tag_mutability = "MUTABLE")
#     - ECS Exec enabled                (ecs_enable_execute_command = true)
#     - Deletion protection off         (db_deletion_protection = false)
#     - 7-day Secrets Manager recovery  (secrets_recovery_window_in_days = 7)
#     - 7-day CloudWatch retention      (log_retention_days = 7)
#     - 1-day RDS backup retention      (db_backup_retention_period = 1)
#     - 256 CPU / 512 MiB memory        (smallest valid Fargate combo)
#     - 1 Fargate task                  (no HA)
#     - No PagerDuty                    (enable_pagerduty_alarms = false)
#     - Tolerant 5xx threshold          (alarm_threshold_5xx_per_5min = 25)
#
# State backend: S3 bucket sales-connections-tfstate-dev plus DynamoDB lock
# table sales-connections-tfstate-locks. Bootstrapped once per AWS account
# per the runbook in docs/operations.md.
#
# Auto-deployed by .github/workflows/cd.yml on every push to main with NO
# manual gate per AAP Sec 0.5.2. Dev is the immediate landing pad for the
# main branch; staging follows on automatic merge; prod requires manual
# approval.
#
# Run from this directory:
#     terraform init
#     terraform plan
#     terraform apply
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
  # AWS account by the script in docs/operations.md. Bucket name is distinct
  # from staging (sales-connections-tfstate-staging) and prod
  # (sales-connections-tfstate-prod) for state isolation.
  ##############################################################################
  backend "s3" {
    bucket         = "sales-connections-tfstate-dev"
    key            = "sales-connections/dev/terraform.tfstate"
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
  description = "Backend container image tag to deploy (CI sets this to the git SHA on every push to main; defaults to 'latest' for bootstrap)."
  type        = string
  default     = "latest"
}

variable "frontend_image_tag" {
  description = "Frontend container image tag to deploy (CI sets this to the git SHA on every push to main; defaults to 'latest' for bootstrap)."
  type        = string
  default     = "latest"
}

variable "github_actions_deploy_role_arn" {
  description = "ARN of the IAM role assumed by GitHub Actions via OIDC for terraform apply against dev. Empty string falls back to the standard AWS provider chain for local development."
  type        = string
  default     = ""
}

variable "assume_role_external_id" {
  description = "External ID for the OIDC assume_role call (defense-in-depth against confused deputy). Empty string disables external_id."
  type        = string
  default     = ""
  sensitive   = true
}

variable "acm_certificate_arn" {
  description = "ARN of an existing ACM certificate covering var.domain_name. Empty string causes the ALB module to provision a new certificate via DNS validation when var.domain_name is also set."
  type        = string
  default     = ""
}

variable "domain_name" {
  description = "Public domain for the dev application (e.g., 'app.dev.example.com'). Empty string causes application_url to fall back to the raw ALB DNS name."
  type        = string
  default     = ""
}

variable "alarm_email_addresses" {
  description = "Email addresses subscribed to the SNS alarm topic for dev. Optional in dev; useful for early-warning visibility during iteration."
  type        = list(string)
  default     = []
}

###############################################################################
# Stack module - the dev composition body
#
# Calls the root composition at ../../ and applies dev-grade values. Every
# attribute here resolves to a `variable` block declared in
# infra/terraform/variables.tf and is validated at plan time.
###############################################################################

module "stack" {
  source = "../../"

  # Environment metadata.
  aws_region            = "us-east-1"
  environment           = "dev"
  repository_identifier = "blitzy/sales-connections"
  cost_center           = "engineering-platform"
  cost_center_owner     = "platform@example.com"

  # OIDC trust - long-lived AWS keys forbidden per AAP Sec 0.7.4.
  github_actions_deploy_role_arn = var.github_actions_deploy_role_arn
  assume_role_external_id        = var.assume_role_external_id

  # Network: 2 AZs for cost optimization. CIDR 10.10.0.0/16 is non-overlapping
  # with staging (10.20.0.0/16) and prod (10.30.0.0/16) for future VPC peering.
  # single_nat_gateway = true breaks AZ-failure tolerance for outbound traffic;
  # acceptable in dev only.
  vpc_cidr             = "10.10.0.0/16"
  availability_zones   = ["us-east-1a", "us-east-1b"]
  public_subnet_cidrs  = ["10.10.0.0/24", "10.10.1.0/24"]
  private_subnet_cidrs = ["10.10.10.0/24", "10.10.11.0/24"]
  enable_nat_gateway   = true
  single_nat_gateway   = true # cost optimization for dev

  # Database: small burstable instance, single-AZ. NOT a production pattern;
  # production MUST set db_multi_az = true per AAP Sec 0.4.7.
  db_engine_version               = "17.7"
  db_instance_class               = "db.t4g.medium"
  db_allocated_storage            = 20
  db_max_allocated_storage        = 100
  db_multi_az                     = false # dev-only; production MUST be true
  db_backup_retention_period      = 1
  db_deletion_protection          = false # allows quick tear-down in dev
  db_performance_insights_enabled = true
  db_database_name                = "sales_connections"
  db_master_username              = "sales_connections_app"

  # ECR: MUTABLE tags so developers can overwrite during iteration.
  # Production REQUIRES IMMUTABLE per the parent folder spec.
  ecr_image_tag_mutability    = "MUTABLE"
  ecr_lifecycle_keep_count    = 10
  ecr_lifecycle_untagged_days = 14

  # ECS (Fargate): smallest valid combo 256 CPU / 512 MiB; one task.
  # ECS Exec enabled for live debugging in dev only.
  backend_image_tag          = var.backend_image_tag
  frontend_image_tag         = var.frontend_image_tag
  frontend_delivery_mode     = "container"
  ecs_task_cpu               = 256
  ecs_task_memory            = 512
  ecs_service_desired_count  = 1
  backend_container_port     = 8000
  frontend_container_port    = 80
  ecs_enable_execute_command = true # debug-friendly; OFF in prod

  # Backend application configuration - non-secret env vars only.
  # flask_env=development and log_level=DEBUG surface verbose diagnostics
  # during iteration; staging and prod use production / INFO.
  flask_env                  = "development"
  log_level                  = "DEBUG"
  cors_allowed_origins       = ""
  default_org_id             = "00000000-0000-0000-0000-000000000001"
  default_new_user_role      = "Contributor"
  anthropic_model            = "claude-sonnet-4-5"
  ai_request_timeout_seconds = 5
  otlp_exporter_endpoint     = ""

  # ALB / TLS - dev uses raw ALB DNS unless an operator provides a domain.
  # Access logs enabled to mirror prod's audit trail for parity testing.
  domain_name              = var.domain_name
  acm_certificate_arn      = var.acm_certificate_arn
  alternative_domain_names = []
  alb_access_logs_enabled  = true

  # Secrets Manager - 7-day recovery window (vs. 30 in prod) for quick
  # tear-down. Default AWS-managed KMS key acceptable per AAP Sec 0.4.9.
  secrets_kms_key_id              = ""
  enable_db_password_rotation     = false
  db_password_rotation_lambda_arn = ""
  secrets_recovery_window_in_days = 7

  # Observability - 7-day retention; no PagerDuty in dev. Higher 5xx
  # tolerance to avoid noisy alarms during iteration.
  log_retention_days           = 7
  alarm_email_addresses        = var.alarm_email_addresses
  enable_pagerduty_alarms      = false
  pagerduty_endpoint           = ""
  alarm_threshold_5xx_per_5min = 25

  additional_tags = {
    Tier    = "dev"
    SLATier = "best-effort"
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

output "application_url" {
  description = "Public URL where the dev application can be reached over HTTPS. Uses var.domain_name when set; falls back to the raw ALB DNS otherwise."
  value       = module.stack.application_url
}

output "alb_dns_name" {
  description = "DNS name of the dev ALB. CNAME this in Route53 to var.domain_name (typically done out-of-band on first apply)."
  value       = module.stack.alb_dns_name
}

output "ecr_backend_repository_url" {
  description = "URL of the dev backend ECR repository. Consumed by .github/workflows/cd.yml for image push on every push to main."
  value       = module.stack.ecr_backend_repository_url
}

output "ecr_frontend_repository_url" {
  description = "URL of the dev frontend ECR repository. Consumed by .github/workflows/cd.yml for nginx image push when frontend_delivery_mode == 'container'."
  value       = module.stack.ecr_frontend_repository_url
}

output "rds_endpoint" {
  description = "RDS PostgreSQL writer endpoint in 'host:port' form. The application constructs the SQLAlchemy DSN from this plus credentials sourced from Secrets Manager at task launch."
  value       = module.stack.rds_endpoint
}

output "ecs_cluster_name" {
  description = "ECS cluster name hosting the dev Fargate service. Used by operational tooling such as `aws ecs describe-services`."
  value       = module.stack.ecs_cluster_name
}

output "ecs_service_name" {
  description = "Name of the dev backend ECS service. Used by the CD pipeline for `aws ecs update-service` to roll out new task definitions."
  value       = module.stack.ecs_service_name
}

output "cloudwatch_dashboard_url" {
  description = "Direct URL to the dev CloudWatch dashboard (region-aware). Operators paste this URL into incident channels and runbooks during dev-environment incident response."
  value       = module.stack.cloudwatch_dashboard_url
}
