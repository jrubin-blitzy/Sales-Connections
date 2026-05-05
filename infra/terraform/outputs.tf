###############################################################################
# infra/terraform/outputs.tf
#
# Outputs surfaced by the Sales-Connections root composition. Consumers:
#   - GitHub Actions CD pipeline for post-apply verification
#     (.github/workflows/cd.yml uses ALB DNS + ECR repository URLs)
#   - Operations runbook (docs/operations.md) which references these output
#     names for deploys, rollbacks, and incident response
#   - Per-environment compositions (envs/{dev,staging,prod}/main.tf) that
#     re-export selected values from this stack module
#   - Local developers via `terraform output` after `terraform apply`
#
# CRITICAL: NEVER output secret VALUES. Output only ARNs/IDs/endpoints that
# point to where secrets live. The ECS task IAM role reads secret values at
# runtime via secretsmanager:GetSecretValue. The Terraform state file MUST
# NEVER contain a secret plaintext.
# (Per AAP Sec 0.7.4 Security Invariants.)
#
# Output value types:
#   - string  for ARNs, DNS names, IDs, URLs, names
#   - list    for subnet IDs, availability zones
#   - map     for environment metadata
#
# `sensitive = true` is documented Terraform but is NOT used here because
# ARNs/IDs/DNS names are reference identifiers, not secrets. They appear in
# CloudTrail, IAM policies, and runbook documentation as a matter of course.
# Marking them sensitive would suppress useful console output during apply.
#
# File ordering mirrors the module-application order in main.tf and the
# build-layer sequence from AAP Sec 0.5.2:
#   network -> database -> ecr -> secrets -> ecs -> alb -> observability
###############################################################################

###############################################################################
# Network outputs
#
# VPC and subnet identifiers consumed by other tooling and surfaced for
# diagnostic reference. The public subnet list is consumed by the ALB; the
# private subnet list is consumed by ECS tasks and RDS.
###############################################################################

output "vpc_id" {
  description = "ID of the VPC hosting all Sales-Connections infrastructure (e.g., 'vpc-0123456789abcdef0'). Surfaced for diagnostic reference and per-environment compositions."
  value       = module.network.vpc_id
}

output "vpc_cidr" {
  description = "Primary CIDR block of the VPC (e.g., '10.10.0.0/16'). Surfaced for VPC peering planning and security group ingress rule design."
  value       = module.network.vpc_cidr
}

output "public_subnet_ids" {
  description = "List of public subnet IDs across all configured availability zones. Consumed by the ALB for cross-AZ load balancing."
  value       = module.network.public_subnet_ids
}

output "private_subnet_ids" {
  description = "List of private subnet IDs across all configured availability zones. Consumed by ECS Fargate tasks and the RDS DB subnet group."
  value       = module.network.private_subnet_ids
}

output "availability_zones" {
  description = "Availability zones spanned by the network module (e.g., ['us-east-1a', 'us-east-1b']). Surfaced for capacity-planning diagnostics."
  value       = module.network.availability_zones
}

###############################################################################
# Database outputs (RDS PostgreSQL 17.7)
#
# RDS endpoint, port, database name, and operational identifiers. The master
# password is NOT outputted here; it lives in Secrets Manager and is fetched
# at runtime by the ECS task via the db_password_arn output below. The
# application constructs the SQLAlchemy DSN at process startup from these
# non-secret components plus the password injected via the ECS secrets block.
###############################################################################

output "rds_endpoint" {
  description = "RDS PostgreSQL writer endpoint in 'host:port' form (e.g., 'sales-connections-prod-postgres.abcdef.us-east-1.rds.amazonaws.com:5432'). The application constructs the DSN using this plus credentials from Secrets Manager."
  value       = module.database.endpoint
  sensitive   = false
}

output "rds_port" {
  description = "RDS PostgreSQL listening port (always 5432 for standard PostgreSQL configuration)."
  value       = module.database.port
}

output "rds_database_name" {
  description = "Name of the application database (defaults to 'sales_connections'). Used by SQLAlchemy DSN composition and by docs/operations.md."
  value       = module.database.database_name
}

output "rds_security_group_id" {
  description = "Security group ID attached to the RDS instance. Used by ECS tasks for ingress rule construction; the RDS SG accepts ingress on port 5432 only from the ECS task SG."
  value       = module.database.security_group_id
}

output "rds_instance_id" {
  description = "RDS instance identifier (e.g., 'sales-connections-prod-postgres'). Used by operations tooling, CloudWatch dashboards, and incident-response runbooks."
  value       = module.database.instance_id
}

output "rds_db_subnet_group" {
  description = "Name of the DB subnet group (private subnets) hosting the RDS instance. Surfaced for diagnostic reference and for re-attaching read replicas in post-MVP work."
  value       = module.database.db_subnet_group_name
}

###############################################################################
# ECR outputs
#
# Container image repository URLs and ARNs. The CD pipeline uses the
# repository URLs to tag and push images via `docker push`; the ARNs are
# referenced in the ECS execution role's `ecr:GetDownloadUrlForLayer`
# permission scope. Frontend repository is populated only when the frontend
# is delivered as an nginx container (vs. S3+CloudFront static delivery).
###############################################################################

output "ecr_backend_repository_url" {
  description = "URL of the backend ECR repository (e.g., '123456789012.dkr.ecr.us-east-1.amazonaws.com/sales-connections-backend'). Consumed by .github/workflows/cd.yml for image push."
  value       = module.ecr.backend_repository_url
}

output "ecr_backend_repository_arn" {
  description = "ARN of the backend ECR repository. Used by the ECS task execution role for pull permissions (per the two-role separation in AAP Sec 0.4.6)."
  value       = module.ecr.backend_repository_arn
}

output "ecr_frontend_repository_url" {
  description = "URL of the frontend ECR repository. Populated only when frontend_delivery_mode == 'container' (vs. S3+CloudFront static); empty string otherwise."
  value       = module.ecr.frontend_repository_url
}

output "ecr_frontend_repository_arn" {
  description = "ARN of the frontend ECR repository. Used by the ECS task execution role for pull permissions when frontend container deployment is enabled; empty string when frontend is delivered as static assets."
  value       = module.ecr.frontend_repository_arn
}

###############################################################################
# Secrets Manager outputs
#
# CRITICAL: These outputs surface ARNs ONLY, never values. The ECS task IAM
# role uses these ARNs in its `secretsmanager:GetSecretValue` permission
# scope and resolves the values at task launch via the awslogs driver and
# the task definition's `secrets` block. Plaintext credentials never appear
# in Terraform state, plan output, or CloudTrail logs.
#
# Secrets surfaced (per AAP Sec 0.4.6):
#   - ANTHROPIC_API_KEY              (F-002 AI note generation)
#   - GOOGLE_OAUTH_CLIENT_SECRET     (F-012 OAuth login)
#   - JWT_SIGNING_KEY                (F-012 session token HS256 secret)
#   - DB_PASSWORD                    (RDS master password for DSN composition)
###############################################################################

output "secrets_manager_anthropic_api_key_arn" {
  description = "ARN of the Secrets Manager entry for ANTHROPIC_API_KEY. The ECS task role reads this at startup to invoke Anthropic Claude API. NEVER expose the value itself; only this ARN is safe to surface."
  value       = module.secrets.anthropic_api_key_arn
}

output "secrets_manager_google_oauth_client_secret_arn" {
  description = "ARN of the Secrets Manager entry for GOOGLE_OAUTH_CLIENT_SECRET. Read by the ECS task at OAuth-flow startup for the Authlib code-exchange step. NEVER expose the value itself."
  value       = module.secrets.google_oauth_client_secret_arn
}

output "secrets_manager_jwt_signing_key_arn" {
  description = "ARN of the Secrets Manager entry for JWT_SIGNING_KEY (HS256 secret used by PyJWT to mint and verify session tokens). Read by the ECS task at boot. NEVER expose the value itself."
  value       = module.secrets.jwt_signing_key_arn
}

output "secrets_manager_db_password_arn" {
  description = "ARN of the Secrets Manager entry for the RDS master password. Read by the ECS task to construct the SQLAlchemy DSN at boot, and by the database module at provision time. NEVER expose the value itself."
  value       = module.secrets.db_password_arn
}

###############################################################################
# ECS outputs (Fargate cluster, service, IAM roles)
#
# Cluster, service, and IAM role identifiers used by operational tooling
# (e.g., `aws ecs update-service`, `aws ecs execute-command`) and by the CD
# pipeline for service-level deployment operations. The two-role separation
# (task role for runtime, execution role for image pull/secret resolution)
# is enforced per AAP Sec 0.4.6.
###############################################################################

output "ecs_cluster_name" {
  description = "ECS cluster name hosting the backend Fargate service (e.g., 'sales-connections-prod'). Used by operational tooling such as `aws ecs describe-services`."
  value       = module.ecs.cluster_name
}

output "ecs_cluster_arn" {
  description = "ARN of the ECS cluster. Used in IAM policy resource scoping and by CloudWatch alarm dimensions."
  value       = module.ecs.cluster_arn
}

output "ecs_service_name" {
  description = "Name of the backend ECS service (e.g., 'sales-connections-prod-backend'). Used by the CD pipeline for `aws ecs update-service` and for forced new deployments."
  value       = module.ecs.service_name
}

output "ecs_task_definition_family" {
  description = "Family name of the backend ECS task definition (e.g., 'sales-connections-prod-backend'). Revisions are bumped on each deploy; the family stays constant."
  value       = module.ecs.task_definition_family
}

output "ecs_migration_task_definition_family" {
  description = "Family name of the one-shot Alembic migration ECS task definition (e.g., 'sales-connections-prod-migration'). Per docs/operations.md the CD pipeline invokes `aws ecs run-task --task-definition <family>` with this family BEFORE the rolling deployment of the long-running backend service so schema migrations apply ahead of new application code. Family is stable across revisions; the CD pipeline registers new revisions on each image bump."
  value       = module.ecs.migration_task_definition_family
}

output "ecs_task_role_arn" {
  description = "IAM task role ARN used by ECS containers at RUNTIME to read secrets and write CloudWatch logs (per the two-role separation in AAP Sec 0.4.6). This role has secretsmanager:GetSecretValue scoped to the four secret ARNs only."
  value       = module.ecs.task_role_arn
}

output "ecs_execution_role_arn" {
  description = "IAM execution role ARN used by ECS at TASK LAUNCH to pull images from ECR and emit logs. This role has ecr:GetDownloadUrlForLayer (NEVER granted to the runtime task role)."
  value       = module.ecs.execution_role_arn
}

output "ecs_security_group_id" {
  description = "Security group ID attached to ECS Fargate tasks. Used by RDS for ingress rules (port 5432 from this SG only) and by the ALB for backend-target ingress on the container port."
  value       = module.ecs.security_group_id
}

###############################################################################
# ALB outputs (Application Load Balancer)
#
# Public entry point for HTTPS traffic. The ALB DNS name should be CNAME'd
# in Route53 to the application's domain. ACM certificate management is
# handled by the ALB module (provisions a fresh cert via DNS validation
# when var.domain_name is set and var.acm_certificate_arn is empty).
###############################################################################

output "alb_dns_name" {
  description = "DNS name of the ALB (e.g., 'sales-connections-prod-1234567890.us-east-1.elb.amazonaws.com'). CNAME this in Route53 to the application's domain."
  value       = module.alb.dns_name
}

output "alb_zone_id" {
  description = "Hosted zone ID for the ALB. Used in Route53 alias records as alias_target.zone_id when configuring custom domain mappings."
  value       = module.alb.zone_id
}

output "alb_arn" {
  description = "ARN of the Application Load Balancer. Used in IAM policy resource scoping and by observability alarms that target this specific ALB."
  value       = module.alb.arn
}

output "alb_listener_https_arn" {
  description = "ARN of the HTTPS:443 listener (TLS termination via ACM certificate). Used by Route53 health checks and by listener-rule modifications in post-MVP work."
  value       = module.alb.listener_https_arn
}

output "alb_target_group_arn" {
  description = "ARN of the target group registered with the backend ECS service. Used by the ECS service definition's load_balancer block and by canary/blue-green deployment tooling."
  value       = module.alb.target_group_arn
}

output "application_url" {
  description = "Public URL where the application can be reached over HTTPS. Uses var.domain_name when set (typical for prod and staging); falls back to the raw ALB DNS name (typical for dev). Always a working URL."
  value       = "https://${var.domain_name != "" ? var.domain_name : module.alb.dns_name}"
}

###############################################################################
# Observability outputs
#
# CloudWatch log group names and dashboard URLs for operator convenience.
# The dashboard URL is region-aware so paste-into-runbook flows work
# without manual region edits. The backend log group receives Flask /
# Gunicorn stdout via the awslogs Docker driver in the ECS task definition.
###############################################################################

output "cloudwatch_log_group_backend" {
  description = "CloudWatch log group capturing backend Fargate task logs (Flask + Gunicorn stdout). Consumed by the awslogs driver in the ECS task definition; surfaced for runbook hyperlinks and CLI tail commands."
  value       = module.observability.backend_log_group_name
}

output "cloudwatch_log_group_alb" {
  description = "CloudWatch log group capturing ALB access logs (mirrored from S3 via subscription). Returns empty string when ALB-to-CloudWatch log forwarding is disabled."
  value       = module.observability.alb_log_group_name
}

output "cloudwatch_dashboard_name" {
  description = "Name of the CloudWatch dashboard with the canonical Sales-Connections operational widgets (request rate, P95 latency, AI-call latency, audit-emit latency, RDS metrics, ECS metrics)."
  value       = module.observability.dashboard_name
}

output "cloudwatch_dashboard_url" {
  description = "Direct URL to the CloudWatch dashboard (region-aware). Operators paste this URL into incident channels and runbooks; it requires the operator to be authenticated to AWS console with sufficient CloudWatch:ListDashboards permissions."
  value       = "https://console.aws.amazon.com/cloudwatch/home?region=${var.aws_region}#dashboards:name=${module.observability.dashboard_name}"
}

###############################################################################
# Deployment metadata
#
# Static descriptive map for use in the executive deck (blitzy-deck/index.html),
# the operations runbook (docs/operations.md), and per-environment composition
# debug output. Keys mirror the canonical project tagging convention so
# downstream consumers can construct CloudWatch and CloudTrail filters
# without rebinding to per-output values.
###############################################################################

output "deployment_metadata" {
  description = "Static metadata describing this deployment. Keys: environment (dev|staging|prod), region (AWS region), project (sales-connections), managed_by (Terraform). Surfaced for runbooks, dashboards, and the executive deck."
  value = {
    environment = var.environment
    region      = var.aws_region
    project     = "sales-connections"
    managed_by  = "Terraform"
  }
}
