###############################################################################
# infra/terraform/envs/staging/main.tf
#
# Staging environment composition for the Sales-Connections platform.
# Wraps the root composition at ../../ (infra/terraform/) as a stack module
# and applies staging-grade variable values to it.
#
# Staging is the PRE-PRODUCTION VALIDATION environment. Its purpose is to
# catch issues that only surface in a production-like topology (Multi-AZ
# behavior, IMMUTABLE tag enforcement, full audit retention, observability
# fan-out) BEFORE they reach prod. Therefore staging mirrors prod's HA
# features even though its scale is intentionally smaller:
#
#   Production-like settings (NON-NEGOTIABLE per parent folder spec):
#     - Multi-AZ RDS                    (db_multi_az = true)
#     - Multi-AZ NAT gateway            (single_nat_gateway = false)
#     - IMMUTABLE ECR tags              (ecr_image_tag_mutability = "IMMUTABLE")
#     - ECS Exec disabled               (ecs_enable_execute_command = false)
#     - Deletion protection on          (db_deletion_protection = true)
#     - 30-day Secrets Manager recovery (secrets_recovery_window_in_days = 30)
#
#   Smaller-scale settings (vs. prod):
#     - db.t4g.large RDS instance       (prod: db.m7g.large)
#     - 2 Fargate tasks                 (prod: 3)
#     - 512 CPU / 1024 MiB memory       (prod: 1024 / 2048)
#     - 7-day RDS backup retention      (prod: 30)
#     - 30-day CloudWatch retention     (prod: 90)
#     - PagerDuty optional              (prod: mandatory)
#
# State backend: S3 bucket sales-connections-tfstate-staging plus DynamoDB
# lock table sales-connections-tfstate-locks. Bootstrapped once per AWS
# account per the runbook in docs/operations.md.
#
# Auto-deployed by .github/workflows/cd.yml on every merge to main with NO
# manual gate per AAP Sec 0.5.2. This is intentional: staging is the
# automatic checkpoint between PR-merged CI passes and prod (which requires
# manual approval).
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
  # from dev (sales-connections-tfstate-dev) and prod
  # (sales-connections-tfstate-prod) for state isolation.
  ##############################################################################
  backend "s3" {
    bucket         = "sales-connections-tfstate-staging"
    key            = "sales-connections/staging/terraform.tfstate"
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
  description = "Backend container image tag to deploy (CI sets this to the git SHA on every merge to main; defaults to 'latest' for bootstrap)."
  type        = string
  default     = "latest"
}

variable "frontend_image_tag" {
  description = "Frontend container image tag to deploy (CI sets this to the git SHA on every merge to main; defaults to 'latest' for bootstrap)."
  type        = string
  default     = "latest"
}

variable "github_actions_deploy_role_arn" {
  description = "ARN of the IAM role assumed by GitHub Actions via OIDC for terraform apply against staging. Passed via TF_VAR_github_actions_deploy_role_arn from the CD workflow."
  type        = string
  default     = ""
}

variable "assume_role_external_id" {
  description = "External ID for the OIDC assume_role call (defense-in-depth against confused deputy). Passed via TF_VAR_assume_role_external_id; empty string disables external_id."
  type        = string
  default     = ""
  sensitive   = true
}

variable "acm_certificate_arn" {
  description = "ARN of an existing ACM certificate covering var.domain_name. Empty string causes the ALB module to provision a new certificate via DNS validation."
  type        = string
  default     = ""
}

variable "domain_name" {
  description = "Public domain for the staging application (e.g., 'app.staging.example.com'). Empty string causes application_url to fall back to the raw ALB DNS name."
  type        = string
  default     = ""
}

variable "alarm_email_addresses" {
  description = "Email addresses subscribed to the SNS alarm topic for staging. Optional in staging but recommended for early-warning visibility."
  type        = list(string)
  default     = []
}

variable "enable_pagerduty_alarms" {
  description = "Whether to fan out alarms to PagerDuty (optional in staging; mandatory in prod). When true, var.pagerduty_endpoint must also be supplied."
  type        = bool
  default     = false
}

variable "pagerduty_endpoint" {
  description = "PagerDuty integration endpoint (HTTPS URL). Required only when enable_pagerduty_alarms is true; passed via TF_VAR_pagerduty_endpoint to keep the value out of source control."
  type        = string
  default     = ""
  sensitive   = true
}

###############################################################################
# Stack module - the staging composition body
#
# Calls the root composition at ../../ and applies staging-grade values.
# Every attribute here resolves to a `variable` block declared in
# infra/terraform/variables.tf and is validated at plan time.
###############################################################################

module "stack" {
  source = "../../"

  # Environment metadata.
  aws_region            = "us-east-1"
  environment           = "staging"
  repository_identifier = "blitzy/sales-connections"
  cost_center           = "engineering-platform"
  cost_center_owner     = "platform@example.com"

  # OIDC trust - long-lived AWS keys forbidden per AAP Sec 0.7.4.
  github_actions_deploy_role_arn = var.github_actions_deploy_role_arn
  assume_role_external_id        = var.assume_role_external_id

  # Network: 3 AZs for production-like HA. Multi-AZ NAT gateway.
  # CIDR 10.20.0.0/16 is non-overlapping with dev (10.10.0.0/16) and prod
  # (10.30.0.0/16) to allow VPC peering later if needed.
  vpc_cidr             = "10.20.0.0/16"
  availability_zones   = ["us-east-1a", "us-east-1b", "us-east-1c"]
  public_subnet_cidrs  = ["10.20.0.0/24", "10.20.1.0/24", "10.20.2.0/24"]
  private_subnet_cidrs = ["10.20.10.0/24", "10.20.11.0/24", "10.20.12.0/24"]
  enable_nat_gateway   = true
  single_nat_gateway   = false

  # Database: production-like Multi-AZ; mid-tier db.t4g.large Graviton.
  db_engine_version               = "17.7"
  db_instance_class               = "db.t4g.large"
  db_allocated_storage            = 50
  db_max_allocated_storage        = 200
  db_multi_az                     = true
  db_backup_retention_period      = 7
  db_deletion_protection          = true
  db_performance_insights_enabled = true
  db_database_name                = "sales_connections"
  db_master_username              = "sales_connections_app"

  # ECR: IMMUTABLE tags for production-grade traceability and rollback safety.
  ecr_image_tag_mutability    = "IMMUTABLE"
  ecr_lifecycle_keep_count    = 30
  ecr_lifecycle_untagged_days = 7

  # ECS (Fargate): 512 CPU / 1024 MiB; 2 tasks for HA. ECS Exec disabled.
  backend_image_tag          = var.backend_image_tag
  frontend_image_tag         = var.frontend_image_tag
  frontend_delivery_mode     = "container"
  ecs_task_cpu               = 512
  ecs_task_memory            = 1024
  ecs_service_desired_count  = 2
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

  # ALB / TLS - access logs enabled; ACM cert and domain supplied via TF_VAR_*.
  domain_name              = var.domain_name
  acm_certificate_arn      = var.acm_certificate_arn
  alternative_domain_names = []
  alb_access_logs_enabled  = true

  # Secrets Manager - 30-day recovery (max). Rotation deferred per AAP Sec 0.4.9.
  secrets_kms_key_id              = ""
  enable_db_password_rotation     = false
  db_password_rotation_lambda_arn = ""
  secrets_recovery_window_in_days = 30

  # Observability - 30-day retention; PagerDuty optional in staging.
  log_retention_days           = 30
  alarm_email_addresses        = var.alarm_email_addresses
  enable_pagerduty_alarms      = var.enable_pagerduty_alarms
  pagerduty_endpoint           = var.pagerduty_endpoint
  alarm_threshold_5xx_per_5min = 10

  additional_tags = {
    Tier    = "staging"
    SLATier = "pre-production"
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
  description = "Public URL where the staging application can be reached over HTTPS. Uses var.domain_name when set; falls back to the raw ALB DNS otherwise."
  value       = module.stack.application_url
}

output "alb_dns_name" {
  description = "DNS name of the staging ALB. CNAME this in Route53 to var.domain_name (typically done out-of-band on first apply)."
  value       = module.stack.alb_dns_name
}

output "ecr_backend_repository_url" {
  description = "URL of the staging backend ECR repository. Consumed by .github/workflows/cd.yml for image push on every merge to main."
  value       = module.stack.ecr_backend_repository_url
}

output "ecr_frontend_repository_url" {
  description = "URL of the staging frontend ECR repository. Consumed by .github/workflows/cd.yml for nginx image push when frontend_delivery_mode == 'container'."
  value       = module.stack.ecr_frontend_repository_url
}

output "rds_endpoint" {
  description = "RDS PostgreSQL writer endpoint in 'host:port' form. The application constructs the SQLAlchemy DSN from this plus credentials sourced from Secrets Manager at task launch."
  value       = module.stack.rds_endpoint
}

output "ecs_cluster_name" {
  description = "ECS cluster name hosting the staging Fargate service. Used by operational tooling such as `aws ecs describe-services`."
  value       = module.stack.ecs_cluster_name
}

output "ecs_service_name" {
  description = "Name of the staging backend ECS service. Used by the CD pipeline for `aws ecs update-service` to roll out new task definitions."
  value       = module.stack.ecs_service_name
}

output "cloudwatch_dashboard_url" {
  description = "Direct URL to the staging CloudWatch dashboard (region-aware). Operators paste this URL into incident channels and runbooks during staging-environment incident response."
  value       = module.stack.cloudwatch_dashboard_url
}
