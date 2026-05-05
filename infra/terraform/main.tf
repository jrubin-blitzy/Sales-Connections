###############################################################################
# infra/terraform/main.tf
#
# Root composition for the Sales-Connections AWS infrastructure stack.
# This file orchestrates seven child modules in the dependency-driven order
# mandated by AAP Sec 0.5.2:
#
#   1. network        - VPC, public/private subnets, NAT, security groups
#   2. database       - RDS PostgreSQL 17.7 Multi-AZ
#   3. ecr            - Container image repositories with native scanner
#   4. secrets        - Secrets Manager entries (Anthropic key, OAuth, JWT, DB)
#   5. ecs            - Fargate cluster, task definition, service, IAM roles
#   6. alb            - Application Load Balancer with ACM TLS termination
#   7. observability  - CloudWatch log groups, metric alarms, dashboard
#
# This stack is instantiated per environment by:
#   - infra/terraform/envs/dev/main.tf
#   - infra/terraform/envs/staging/main.tf
#   - infra/terraform/envs/prod/main.tf
#
# Each environment overrides variables defined in variables.tf to size
# resources appropriately (e.g., dev uses smaller instances, prod uses
# Multi-AZ + larger task counts).
#
# CROSS-MODULE DEPENDENCY RESOLUTION
# ----------------------------------
# Several module pairs have bidirectional data flows that look like cycles
# at the module reference level. Terraform resolves these by expanding each
# module reference into its underlying resource graph; as long as no
# individual resource has a cyclic dependency, Terraform plans cleanly.
# The resolutions employed here (each documented in docs/decision-log.md):
#
# 1. ECS <-> ALB (security groups + target group)
#    - ECS exposes its tasks security_group_id (no inputs from ALB needed
#      to construct the SG itself).
#    - ALB exposes its security_group_id, target_group_arn, dns_name (no
#      inputs from ECS needed to construct these resources).
#    - Each side's SG rules and ECS load_balancer block only reference
#      attribute IDs of the other side - never the module itself.
#    - Result: aws_security_group.tasks and aws_security_group.alb are
#      created in parallel; SG rules and the ECS service's load_balancer
#      block follow once both SGs exist.
#
# 2. Secrets <-> ECS (resource policy reader principal vs. secret ARNs)
#    - Secrets accepts reader_principal_arns = [module.ecs.task_role_arn].
#    - ECS accepts secret_*_arn = module.secrets.*.
#    - aws_iam_role.task in ECS has no input from secrets. The aws_iam_role
#      resource is created first; its ARN feeds the Secrets resource policy.
#    - aws_secretsmanager_secret.* in Secrets has no input from ECS. The
#      secret resources are created first; their ARNs feed the ECS task
#      definition's `secrets` block.
#    - Result: cycle exists at the module-reference level only; resource
#      graph remains acyclic.
#
# 3. Database <-> Secrets (master password ARN)
#    - Database accepts master_password_secret_arn = module.secrets.db_password_arn.
#    - Secrets does NOT depend on database outputs.
#    - Result: unidirectional, no cycle.
#
# All decisions are recorded in docs/decision-log.md per AAP Sec 0.7.5
# Explainability rule. No rationale is buried in code comments beyond
# cross-references to those records.
#
# FILE-ORDER NOTE
# ---------------
# Terraform builds its dependency graph from attribute references, NOT
# from the textual order in this file. The order of module blocks below
# follows AAP Sec 0.5.2 for human readability and to mirror the seven-
# module conceptual sequence; Terraform itself plans modules in
# dependency-graph order regardless of file layout.
###############################################################################

###############################################################################
# Data sources for AWS account/region context
#
# These four sources establish the canonical identity (account, region,
# partition, AZ list) used by IAM policy ARN construction, resource naming,
# and cross-module attribute composition. They depend only on the AWS
# provider declared in versions.tf and configured in providers.tf.
###############################################################################

data "aws_caller_identity" "current" {}

data "aws_region" "current" {}

data "aws_partition" "current" {}

# Available AZs in the current region. Filtered to opt-in-not-required so
# the slate excludes any AZs that require explicit account opt-in (e.g.,
# certain Local Zones and Wavelength Zones). The network module receives
# the explicit list via var.availability_zones; this data source surfaces
# the regional inventory for diagnostics and future use.
data "aws_availability_zones" "available" {
  state = "available"

  filter {
    name   = "opt-in-status"
    values = ["opt-in-not-required"]
  }
}

###############################################################################
# Locals
#
# All locals here are immutable for the lifetime of an apply. They are
# computed from input variables and the four data sources above.
#
#   account_id / region / partition  -- canonical AWS identity values used
#                                       for IAM ARN composition and
#                                       cross-module attribute references.
#
#   name_prefix                      -- environment-qualified resource
#                                       naming prefix. Every module
#                                       receives this so resource names
#                                       are uniform across environments
#                                       sharing an AWS account (dev,
#                                       staging, prod can coexist).
#
#   deploy_frontend_container        -- true when var.frontend_delivery_mode
#                                       == "container" (drives ECR frontend
#                                       repository creation, ECS frontend
#                                       service, observability frontend
#                                       service identifier).
#
#   deploy_frontend_static           -- true when var.frontend_delivery_mode
#                                       == "static" (post-MVP; documented
#                                       below as a placeholder block).
#
#   effective_public_origin          -- the public origin (scheme + host)
#                                       at which the SPA reaches the API.
#                                       Used to construct the OAuth
#                                       redirect URI fed to ECS. When
#                                       var.domain_name is non-empty,
#                                       https://<domain> is used;
#                                       otherwise the raw ALB DNS is
#                                       used. The latter case requires
#                                       Google OAuth client allowed-
#                                       redirect-URI updates after each
#                                       ALB recreation, which is
#                                       acceptable for dev only.
#
#   google_oauth_redirect_uri        -- the full URL passed to ECS for
#                                       use by Authlib's Google client.
#                                       Constructed once here so the
#                                       value is consistent across the
#                                       composition and a single source
#                                       of truth for changes.
#
#   backend_service_name /
#   frontend_service_name            -- service identifiers shared by
#                                       module.observability and used
#                                       within module.ecs. Computing
#                                       them once here keeps them
#                                       consistent across consumers.
###############################################################################

locals {
  account_id = data.aws_caller_identity.current.account_id
  region     = data.aws_region.current.name
  partition  = data.aws_partition.current.partition

  name_prefix = "sales-connections-${var.environment}"

  deploy_frontend_container = var.frontend_delivery_mode == "container"
  deploy_frontend_static    = var.frontend_delivery_mode == "static"

  # Public origin used for the OAuth redirect URI. The ECS module receives
  # the fully-qualified URI; rebuilding it here keeps the construction
  # deterministic and reviewable in one place. Per AAP Sec 0.4.5 the OAuth
  # callback path is /auth/google/callback served from the same origin as
  # the SPA.
  effective_public_origin = var.domain_name != "" ? "https://${var.domain_name}" : "https://${module.alb.dns_name}"

  google_oauth_redirect_uri = "${local.effective_public_origin}/auth/google/callback"

  # Backend service identifier reused by observability for log-group naming
  # and by the ECS module for task-definition family naming. Keeping a
  # single source of truth prevents drift when new sites consume the name.
  backend_service_name  = "${local.name_prefix}-backend"
  frontend_service_name = local.deploy_frontend_container ? "${local.name_prefix}-frontend" : ""
}

###############################################################################
# Module 1 of 7: network
#
# Foundational module: VPC, public + private subnets across AZs, route
# tables, Internet Gateway, NAT Gateway(s), default-SG lockdown, optional
# VPC endpoints. Every other module references vpc_id and at least one of
# the subnet lists, so this module is the foundational dependency per
# AAP Sec 0.5.2.
#
# Inputs sourced from var.* directly (no upstream module dependencies).
###############################################################################

module "network" {
  source = "./modules/network"

  name_prefix          = local.name_prefix
  environment          = var.environment
  vpc_cidr             = var.vpc_cidr
  availability_zones   = var.availability_zones
  public_subnet_cidrs  = var.public_subnet_cidrs
  private_subnet_cidrs = var.private_subnet_cidrs
  enable_nat_gateway   = var.enable_nat_gateway
  single_nat_gateway   = var.single_nat_gateway

  tags = local.common_tags
}

###############################################################################
# Module 2 of 7: database
#
# RDS PostgreSQL 17.7 Multi-AZ in the private subnets created by
# module.network. The master password ARN flows in from module.secrets;
# the actual password value is resolved by the database module via
# data.aws_secretsmanager_secret_version at apply time and is never
# materialized in plan output (Terraform marks it sensitive).
#
# Cross-module wiring:
#   - vpc_id = module.network.vpc_id
#   - private_subnet_ids = module.network.private_subnet_ids
#   - allowed_security_group_ids = [module.ecs.security_group_id]
#       The RDS security group accepts ingress on port 5432 ONLY from the
#       ECS task SG (zero CIDR-based ingress). This is the network-layer
#       enforcement of "no direct database access from the Internet".
#   - master_password_secret_arn = module.secrets.db_password_arn
#
# The bidirectional ECS<->Database SG cycle is resolved by both sides
# accepting SG IDs as variables (rather than instantiating SGs that
# reference each other). Terraform creates both SGs in parallel and
# applies the ingress rules once both exist.
###############################################################################

module "database" {
  source = "./modules/database"

  name_prefix                = local.name_prefix
  environment                = var.environment
  vpc_id                     = module.network.vpc_id
  private_subnet_ids         = module.network.private_subnet_ids
  allowed_security_group_ids = [module.ecs.security_group_id]

  engine_version               = var.db_engine_version
  instance_class               = var.db_instance_class
  allocated_storage            = var.db_allocated_storage
  max_allocated_storage        = var.db_max_allocated_storage
  multi_az                     = var.db_multi_az
  backup_retention_period      = var.db_backup_retention_period
  deletion_protection          = var.db_deletion_protection
  performance_insights_enabled = var.db_performance_insights_enabled

  database_name              = var.db_database_name
  master_username            = var.db_master_username
  master_password_secret_arn = module.secrets.db_password_arn

  tags = local.common_tags
}

###############################################################################
# Module 3 of 7: ecr
#
# Container image repositories for the backend (always) and the frontend
# (only when var.frontend_delivery_mode == "container"). The ECR native
# scanner is enabled per AAP Sec 0.4.6 so vulnerabilities in pushed images
# surface as CloudWatch findings without requiring a separate scanner.
#
# Cross-module wiring:
#   - push_role_arn = var.github_actions_deploy_role_arn
#       The OIDC-federated GitHub Actions deploy role is the only
#       identity allowed to push images. AAP Sec 0.3.6.
#   - pull_role_arns = [module.ecs.execution_role_arn]
#       The ECS execution role is the only identity allowed to pull
#       images at task launch. AAP Sec 0.4.6 two-role separation.
#   - create_frontend_repo = local.deploy_frontend_container
#       The frontend repo is provisioned only when the container
#       delivery mode is selected; static-mode environments skip it.
###############################################################################

module "ecr" {
  source = "./modules/ecr"

  name_prefix          = local.name_prefix
  environment          = var.environment
  create_frontend_repo = local.deploy_frontend_container

  image_tag_mutability = var.ecr_image_tag_mutability
  scan_on_push         = true
  encryption_type      = "AES256"

  lifecycle_policy_keep_count    = var.ecr_lifecycle_keep_count
  lifecycle_policy_untagged_days = var.ecr_lifecycle_untagged_days

  # Push: GitHub Actions OIDC deploy role (CD pipeline). Empty string
  # acceptable in environments where ECR is bootstrapped before the role
  # exists; the ECR module skips the push statement in that case.
  push_role_arn = var.github_actions_deploy_role_arn

  # Pull: ECS task execution role. The execution role is responsible for
  # image pulls during task launch (the runtime task role NEVER has
  # ecr:GetDownloadUrlForLayer permissions per AAP Sec 0.4.6).
  pull_role_arns = [module.ecs.execution_role_arn]

  tags = local.common_tags
}

###############################################################################
# Module 4 of 7: secrets
#
# Secrets Manager entries for ANTHROPIC_API_KEY, GOOGLE_OAUTH_CLIENT_SECRET,
# JWT_SIGNING_KEY, and the RDS master password (DB_PASSWORD). Values are
# either Terraform-generated (dev) or operator-populated externally (prod);
# in both cases this module owns only the secret entry resources and the
# resource policy granting read access to the ECS task role.
#
# Per AAP Sec 0.4.6, the master password secret ARN is dual-consumer:
# module.database resolves it at provision time, and module.ecs's task
# definition references it for runtime DSN composition.
#
# Cross-module wiring (module-reference cycle, acyclic at resource graph):
#   - reader_principal_arns = [module.ecs.task_role_arn]
#       Only the ECS task role is allowed to read secret values at runtime.
#       This is the AAP Sec 0.7.4 least-privilege invariant.
#   - kms_key_id = var.secrets_kms_key_id (empty by default; AWS-managed key)
#   - rotation_lambda_arn = var.db_password_rotation_lambda_arn (empty by
#       default; rotation Lambda is post-MVP per AAP Sec 0.4.9)
###############################################################################

module "secrets" {
  source = "./modules/secrets"

  name_prefix = local.name_prefix
  environment = var.environment

  # Reader principals: only the ECS task role can fetch secret values at
  # runtime via secretsmanager:GetSecretValue. AAP Sec 0.7.4 least-
  # privilege invariant.
  reader_principal_arns = [module.ecs.task_role_arn]

  # KMS-encrypted; default AWS-managed alias/aws/secretsmanager is
  # acceptable for MVP per AAP Sec 0.4.9. Operators may set
  # var.secrets_kms_key_id to a customer-managed CMK ARN to override.
  kms_key_id = var.secrets_kms_key_id

  # Rotation hooks: enabled for DB password (RDS rotation Lambda); disabled
  # for the long-lived API keys (rotated by hand or out-of-band).
  enable_db_password_rotation = var.enable_db_password_rotation
  rotation_lambda_arn         = var.db_password_rotation_lambda_arn

  recovery_window_in_days = var.secrets_recovery_window_in_days

  tags = local.common_tags
}

###############################################################################
# Module 5 of 7: ecs
#
# Fargate cluster, backend task definition (Flask 3.1.3 + Gunicorn),
# optional frontend task definition (nginx) when container delivery is
# selected, ECS services, security group, and the two-role IAM separation
# (task role for runtime AWS access, execution role for image pulls and
# secret resolution at task start).
#
# Per AAP Sec 0.4.6 the task definition's `secrets` block references the
# four Secrets Manager ARNs from module.secrets; the execution role uses
# secretsmanager:GetSecretValue at task launch to inject the values as
# env vars (ANTHROPIC_API_KEY, GOOGLE_OAUTH_CLIENT_SECRET, JWT_SIGNING_KEY,
# DB_PASSWORD). Plaintext credentials never appear in the task definition.
#
# Cross-module wiring (and the cycles each line resolves):
#   - vpc_id, private_subnet_ids: from module.network (no cycle).
#   - alb_target_group_arn, alb_security_group_id: from module.alb (cycle
#     with the SG rules, resolved at the resource graph level - the SGs
#     are created in parallel and rules apply once both exist).
#   - secret_*_arn: from module.secrets (cycle with task_role_arn,
#     resolved at the resource graph level - aws_iam_role.task and
#     aws_secretsmanager_secret.* are created in parallel).
#   - db_host, db_port, db_name, db_username: from module.database
#     (one-way; the password is sourced separately via secret_db_password_arn).
#   - cloudwatch_log_group_name: from module.observability (one-way).
###############################################################################

module "ecs" {
  source = "./modules/ecs"

  name_prefix        = local.name_prefix
  environment        = var.environment
  vpc_id             = module.network.vpc_id
  private_subnet_ids = module.network.private_subnet_ids

  # ALB wiring (cyclic at module-reference level; acyclic at resource
  # graph level - see header comments).
  alb_target_group_arn  = module.alb.target_group_arn
  alb_security_group_id = module.alb.security_group_id

  # Container images (frontend conditionally constructed; an empty URI
  # flows through harmlessly when the frontend is not deployed because
  # the ECS module gates aws_ecs_task_definition.frontend on
  # var.deploy_frontend_container).
  backend_image_uri      = "${module.ecr.backend_repository_url}:${var.backend_image_tag}"
  backend_container_port = var.backend_container_port

  deploy_frontend_container = local.deploy_frontend_container
  frontend_image_uri        = local.deploy_frontend_container ? "${module.ecr.frontend_repository_url}:${var.frontend_image_tag}" : ""
  frontend_container_port   = var.frontend_container_port

  # Compute sizing per environment (smaller in dev, larger in prod).
  task_cpu               = var.ecs_task_cpu
  task_memory            = var.ecs_task_memory
  desired_count          = var.ecs_service_desired_count
  enable_execute_command = var.ecs_enable_execute_command

  # Secrets Manager ARNs that the execution role resolves at task start.
  # Plaintext credentials never appear in the task definition JSON.
  secret_anthropic_api_key_arn          = module.secrets.anthropic_api_key_arn
  secret_google_oauth_client_secret_arn = module.secrets.google_oauth_client_secret_arn
  secret_jwt_signing_key_arn            = module.secrets.jwt_signing_key_arn
  secret_db_password_arn                = module.secrets.db_password_arn

  # Database connection metadata (non-secret components). The backend
  # composes the SQLAlchemy DSN at process startup from these env vars
  # plus the DB_PASSWORD injected via the secrets block above.
  db_host     = module.database.address
  db_port     = module.database.port
  db_name     = module.database.database_name
  db_username = module.database.master_username

  # Backend application configuration (non-secret env vars). Per AAP
  # Sec 0.4 these flow into the task definition's `environment` block
  # and are visible in the AWS console.
  flask_env                  = var.flask_env
  log_level                  = var.log_level
  log_format                 = "json"
  cors_allowed_origins       = var.cors_allowed_origins
  default_org_id             = var.default_org_id
  default_new_user_role      = var.default_new_user_role
  anthropic_model            = var.anthropic_model
  ai_request_timeout_seconds = var.ai_request_timeout_seconds

  # OAuth redirect URI - constructed as a local above so the value is a
  # single source of truth for the public origin. Required by Authlib's
  # Google OAuth client to validate the callback URL during the code
  # exchange step (AAP Sec 0.4.5).
  google_oauth_redirect_uri = local.google_oauth_redirect_uri

  # Observability wiring. The awslogs Docker driver in the task
  # definition uses backend_log_group_name to route Flask/Gunicorn
  # stdout into CloudWatch. otlp_exporter_endpoint enables the
  # OpenTelemetry SDK to export distributed traces (empty string
  # disables the exporter).
  cloudwatch_log_group_name = module.observability.backend_log_group_name
  otlp_exporter_endpoint    = var.otlp_exporter_endpoint
  otel_service_name         = "sales-connections-api"

  # Liveness probe path. AAP Sec 0.5.2: /healthz returns 200
  # unconditionally without a DB round-trip; readiness probing
  # (which includes a DB ping) is at /readyz and is not used as the
  # container-level health check to avoid flapping on transient hiccups.
  health_check_path = "/healthz"

  tags = local.common_tags
}

###############################################################################
# Module 6 of 7: alb
#
# Application Load Balancer with HTTPS:443 (ACM-issued certificate) and
# HTTP:80 redirect-only listener per AAP Sec 0.4.6. The target group
# registers ECS Fargate tasks via target_type = "ip" (mandatory for
# Fargate). drop_invalid_header_fields and enable_http2 are explicit per
# AAP Sec 0.4 security and performance constraints.
#
# Cross-module wiring:
#   - vpc_id, public_subnet_ids: from module.network (no cycle).
#   - ecs_security_group_id = module.ecs.security_group_id (cyclic at
#     module-reference level; acyclic at resource graph level - the ALB
#     module owns BOTH the alb_egress_to_ecs and ecs_ingress_from_alb
#     rules, breaking the SG cycle without requiring resource-graph
#     coordination).
#   - access_logs_bucket = module.observability.alb_access_logs_bucket
#     (one-way; the bucket exists unconditionally in observability so
#     the ALB module can wire to it without a circular dependency on
#     the ALB's own access_logs_enabled toggle).
#
# ACM certificate sourcing:
#   - When var.acm_certificate_arn is non-empty, that ARN is attached
#     to the HTTPS listener directly.
#   - When var.acm_certificate_arn is empty AND var.domain_name is non-
#     empty, the ALB module provisions a fresh ACM certificate via DNS
#     validation; operators must create the validation records out-of-
#     band (this module does not own Route53 records).
#   - When BOTH are empty, the HTTPS listener creation fails at apply
#     time with a clear precondition error.
###############################################################################

module "alb" {
  source = "./modules/alb"

  name_prefix       = local.name_prefix
  environment       = var.environment
  vpc_id            = module.network.vpc_id
  public_subnet_ids = module.network.public_subnet_ids

  # ECS SG for ALB->ECS traffic on the backend container port. The ALB
  # module owns both the alb_egress_to_ecs rule (on the ALB SG) and the
  # ecs_ingress_from_alb rule (on the ECS tasks SG); centralizing
  # ownership here breaks the SG circular dependency.
  ecs_security_group_id = module.ecs.security_group_id

  # ACM certificate sourcing - module provisions a cert when ARN empty
  # AND domain non-empty, otherwise uses the supplied ARN as-is.
  acm_certificate_arn      = var.acm_certificate_arn
  domain_name              = var.domain_name
  alternative_domain_names = var.alternative_domain_names

  # Backend target group + health check - hits /healthz (unconditional
  # liveness) per AAP Sec 0.5.2, NOT /readyz (which includes a DB ping
  # that would cause flapping on transient hiccups).
  backend_target_port                      = var.backend_container_port
  backend_health_check_path                = "/healthz"
  backend_health_check_interval            = 30
  backend_health_check_healthy_threshold   = 2
  backend_health_check_unhealthy_threshold = 3
  backend_health_check_timeout             = 5
  backend_health_check_matcher             = "200"

  # Public ingress: full Internet by default; operators may tighten via
  # var override (e.g., behind a corporate WAF for staging).
  allowed_ingress_cidrs = ["0.0.0.0/0"]

  # ALB tuning per AAP Sec 0.4 / 0.7.3:
  #   - idle_timeout 60s covers the 5s AI budget plus margin.
  #   - drop_invalid_header_fields = true (header smuggling defense;
  #     AAP Sec 0.7.4 security invariant).
  #   - enable_http2 = true (SPA performance; AAP Sec 0.4 SPA stack).
  idle_timeout               = 60
  drop_invalid_header_fields = true
  enable_http2               = true

  # Access log delivery. The bucket lives in module.observability and is
  # created unconditionally so this module can wire to it without
  # circular dependencies; the access_logs_enabled toggle below governs
  # whether logs are actually written.
  access_logs_bucket  = module.observability.alb_access_logs_bucket
  access_logs_enabled = var.alb_access_logs_enabled

  tags = local.common_tags
}

###############################################################################
# Module 7 of 7: observability
#
# CloudWatch log groups (backend, optional ALB), metric alarms, dashboard,
# SNS alarm topic with email/PagerDuty fan-out, and the ALB access logs
# S3 bucket. Per AAP Sec 0.7.5 the observability deliverable is what
# turns "the application" into "an observable application".
#
# Alarm thresholds are bound to AAP Sec 0.7.3 performance budgets:
#   - alarm_threshold_p95_ms_form_submit  = 2000  (2 s budget)
#   - alarm_threshold_p95_ms_ai_call      = 5000  (5 s P95 AI budget)
#   - alarm_threshold_audit_emit_ms       = 100   (100 ms audit budget)
#   - alarm_threshold_5xx_per_5min        = configurable per env
#
# Cross-module wiring:
#   - ecs_cluster_name, ecs_service_name = module.ecs.cluster_name /
#     service_name (CloudWatch alarm dimensions for ECS metrics).
#   - rds_instance_id = module.database.instance_id (CloudWatch alarm
#     dimensions for RDS metrics).
#   - alb_arn_suffix, alb_target_group_arn_suffix = module.alb.* (the
#     CloudWatch dimension form, NOT full ARNs).
#   - region = local.region (constructs region-aware dashboard URL).
###############################################################################

module "observability" {
  source = "./modules/observability"

  name_prefix        = local.name_prefix
  environment        = var.environment
  region             = local.region
  log_retention_days = var.log_retention_days

  # Service identifiers used in dashboard widget titles, log group names,
  # and alarm tags. Computed once as locals so the same string is used
  # everywhere.
  backend_service_name  = local.backend_service_name
  frontend_service_name = local.frontend_service_name

  # Alarm fan-out subscribers.
  alarm_email_addresses = var.alarm_email_addresses
  enable_pagerduty      = var.enable_pagerduty_alarms
  pagerduty_endpoint    = var.pagerduty_endpoint

  # Performance-budget thresholds per AAP Sec 0.7.3.
  alarm_threshold_p95_ms_form_submit = 2000
  alarm_threshold_p95_ms_ai_call     = 5000
  alarm_threshold_5xx_per_5min       = var.alarm_threshold_5xx_per_5min
  alarm_threshold_audit_emit_ms      = 100

  # CloudWatch alarm dimensions (sourced from sibling modules).
  ecs_cluster_name            = module.ecs.cluster_name
  ecs_service_name            = module.ecs.service_name
  ecs_service_desired_count   = var.ecs_service_desired_count
  rds_instance_id             = module.database.instance_id
  alb_arn_suffix              = module.alb.arn_suffix
  alb_target_group_arn_suffix = module.alb.target_group_arn_suffix

  tags = local.common_tags
}

###############################################################################
# Optional: Static SPA delivery via S3 + CloudFront
#
# Activated by var.frontend_delivery_mode = "static" (which sets
# local.deploy_frontend_static = true). Per AAP Sec 0.6.1 the seven modules
# above constitute the in-scope MVP infrastructure; a dedicated
# modules/static_frontend/ would be scope creep at this stage. This block
# is a documented placeholder so a future contributor knows where the
# integration would land without searching for the right place.
#
# When implementing later:
#   module "static_frontend" {
#     count    = local.deploy_frontend_static ? 1 : 0
#     source   = "./modules/static_frontend"
#     providers = {
#       aws.us_east_1 = aws.us_east_1   # ACM-for-CloudFront must be us-east-1
#     }
#     name_prefix         = local.name_prefix
#     environment         = var.environment
#     domain_name         = var.domain_name
#     acm_certificate_arn = var.acm_certificate_arn
#     ...
#   }
#
# For the time being, deploy_frontend_static is exposed as a constant
# (see infra/terraform/outputs.tf) so per-environment compositions can
# read it for operator messaging without yet wiring the module itself.
###############################################################################
