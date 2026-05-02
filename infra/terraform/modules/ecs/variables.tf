###############################################################################
# infra/terraform/modules/ecs/variables.tf
#
# Input variables for the Sales-Connections ECS Fargate compute module. Drives
# the cluster, task definitions, service, security group, and IAM role
# configurations.
#
# Variable groups (in declaration order below):
#    1. Identity                     : name_prefix, environment, tags
#    2. Network plumbing             : vpc_id, private_subnet_ids,
#                                      alb_target_group_arn,
#                                      alb_security_group_id
#    3. Container images             : backend_image_uri,
#                                      deploy_frontend_container,
#                                      frontend_image_uri,
#                                      frontend_backend_api_base_url
#    4. Container ports              : backend_container_port,
#                                      frontend_container_port
#    5. Compute sizing               : task_cpu, task_memory, desired_count
#    6. Operational toggles          : enable_execute_command,
#                                      secrets_kms_key_id
#    7. Secret ARNs                  : 4 entries from modules/secrets/
#    8. Database connection          : db_host, db_port, db_name, db_username
#                                      (non-secret components only)
#    9. Backend application config   : flask_env, log_level, log_format,
#                                      cors_allowed_origins, default_org_id,
#                                      default_new_user_role, anthropic_model,
#                                      ai_request_timeout_seconds,
#                                      google_oauth_redirect_uri
#   10. Observability                : cloudwatch_log_group_name,
#                                      otlp_exporter_endpoint, otel_service_name
#   11. Health check                 : health_check_path
#
# Validation strategy:
#   - ARN-formatted variables validated via regex; the partition is matched as
#     [a-z0-9-]+ to support standard AWS ("aws"), GovCloud ("aws-us-gov"),
#     and China ("aws-cn") partitions without modification.
#   - Enum-valued variables validated via contains([...], var.X).
#   - Numeric ranges validated via comparison.
#   - All validation runs at plan time so misconfiguration is caught before
#     apply (fail-fast feedback loop).
#
# Default-value policy (per AAP Sec 0.6.1 critical constraints):
#   - task_cpu                 = 512   (Fargate quantum: 0.5 vCPU)
#   - task_memory              = 1024  (Fargate-compatible with 512 CPU)
#   - desired_count            = 2     (AZ redundancy minimum)
#   - backend_container_port   = 8000  (Gunicorn convention)
#   - frontend_container_port  = 80    (nginx default)
#   - enable_execute_command   = false (security invariant - opt-in only)
#   - health_check_path        = /healthz (liveness probe per AAP Sec 0.5.2)
#   - flask_env                = production
#   - log_level                = INFO
#   - log_format               = json
#   - default_new_user_role    = Contributor (least privilege)
#   - anthropic_model          = claude-sonnet-4-5
#   - ai_request_timeout_seconds = 5 (matches AAP Sec 0.7.3 P95 budget)
#   - otel_service_name        = sales-connections-api
#
# Cross-variable validation note:
#   Terraform 1.7 (the project's pinned floor per versions.tf) does not allow
#   cross-variable references inside a variable's validation block. Pairs that
#   require cross-checking (e.g., task_cpu / task_memory must be a valid
#   Fargate combination, frontend_image_uri must be non-empty when
#   deploy_frontend_container = true) are enforced via lifecycle.precondition
#   on the relevant resources in main.tf or by AWS API rejection at apply time.
###############################################################################

###############################################################################
# 1. Identity
###############################################################################

variable "name_prefix" {
  description = "Naming prefix applied to all ECS resources (cluster, services, task definitions, IAM roles, security groups). Should include the project and environment, e.g., 'sales-connections-prod'. Composed by the parent as '$${var.project}-$${var.environment}' and reused across modules to keep tag and Name labels consistent."
  type        = string

  validation {
    condition     = length(var.name_prefix) > 0 && length(var.name_prefix) <= 30
    error_message = "name_prefix must be 1-30 characters (leaves room for resource-specific suffixes such as '-cluster', '-task-role', '-tasks-sg' within AWS resource name limits)."
  }

  validation {
    condition     = can(regex("^[a-z0-9][a-z0-9-]*[a-z0-9]$", var.name_prefix))
    error_message = "name_prefix must be lowercase alphanumeric with internal hyphens (start and end with letter or digit), e.g., 'sales-connections-prod'."
  }
}

variable "environment" {
  description = "Deployment environment label (one of: dev, staging, prod). Drives Environment tag values for cost-explorer drill-down and feeds into the Component-tag breakdown alongside name_prefix."
  type        = string

  validation {
    condition     = contains(["dev", "staging", "prod"], var.environment)
    error_message = "environment must be one of: dev, staging, prod. Adding a new environment requires explicit operator action (update validation rule + decision-log entry per AAP Sec 0.7.5 Explainability rule)."
  }
}

variable "tags" {
  description = "Map of additional tags merged into every taggable resource created by this module. The module layers in 'Component', 'Environment', 'ManagedBy', and 'Module' tags automatically (in locals.tf) - those module-internal tags cannot be overridden via this variable. The parent composition's provider-level default_tags also apply via AWS provider 5.x default_tags."
  type        = map(string)
  default     = {}
}

###############################################################################
# 2. Network plumbing (consumed from modules/network/ and modules/alb/)
###############################################################################

variable "vpc_id" {
  description = "VPC identifier where ECS tasks and the tasks security group are deployed. Must be the same VPC as the ALB and the RDS instance to allow same-VPC private traffic. Sourced from module.network.vpc_id in the root composition."
  type        = string

  validation {
    condition     = can(regex("^vpc-[0-9a-f]{8,17}$", var.vpc_id))
    error_message = "vpc_id must be a valid VPC ID matching 'vpc-XXXXXXXX' (8-17 hex characters)."
  }
}

variable "private_subnet_ids" {
  description = "List of private subnet IDs for ECS task placement. Tasks have no public IP (assign_public_ip = false in main.tf); they reach the public Internet via NAT gateway and reach Anthropic / Google via NAT or VPC endpoints. Must span at least 2 AZs so the ALB can register healthy targets across availability zones. Sourced from module.network.private_subnet_ids in the root composition."
  type        = list(string)

  validation {
    condition     = length(var.private_subnet_ids) >= 2
    error_message = "private_subnet_ids must contain at least 2 subnets across distinct availability zones (ALB target health and ECS spread placement requirement)."
  }

  validation {
    condition = alltrue([
      for s in var.private_subnet_ids : can(regex("^subnet-[0-9a-f]{8,17}$", s))
    ])
    error_message = "Every entry in private_subnet_ids must be a valid subnet ID matching 'subnet-XXXXXXXX' (8-17 hex characters)."
  }
}

variable "alb_target_group_arn" {
  description = "ARN of the ALB target group that the backend ECS service registers with. The aws_ecs_service.backend resource's load_balancer block references this; ALB health checks then hit the backend container's health_check_path. Sourced from module.alb.target_group_arn in the root composition."
  type        = string

  validation {
    condition     = can(regex("^arn:[a-z0-9-]+:elasticloadbalancing:[a-z0-9-]+:[0-9]+:targetgroup/", var.alb_target_group_arn))
    error_message = "alb_target_group_arn must be a valid ELBv2 target group ARN matching 'arn:<partition>:elasticloadbalancing:<region>:<account>:targetgroup/...'."
  }
}

variable "alb_security_group_id" {
  description = "Security group ID of the ALB. The tasks security group accepts inbound traffic on backend_container_port from THIS security group only (not from arbitrary CIDR blocks), enforcing ALB-only ingress per the least-privilege network model. Sourced from module.alb.security_group_id in the root composition."
  type        = string

  validation {
    condition     = can(regex("^sg-[0-9a-f]{8,17}$", var.alb_security_group_id))
    error_message = "alb_security_group_id must be a valid security group ID matching 'sg-XXXXXXXX' (8-17 hex characters)."
  }
}

###############################################################################
# 3. Container images
###############################################################################

variable "backend_image_uri" {
  description = "Full ECR URI for the backend container image, including tag (e.g., '123456789012.dkr.ecr.us-east-1.amazonaws.com/sales-connections-backend:abc123'). The CD pipeline (.github/workflows/cd.yml) overrides this on each deploy by passing the freshly-built image tag via -var. Required input."
  type        = string

  validation {
    condition     = length(var.backend_image_uri) > 0
    error_message = "backend_image_uri must be non-empty. Supply the full ECR URI including a tag (do NOT use ':latest' in production - reproducible deploys require an immutable tag like a git SHA)."
  }
}

variable "deploy_frontend_container" {
  description = "When true, deploy the frontend nginx container as a separate ECS service alongside the backend. When false (default), the frontend is served via S3 + CloudFront (configured outside this module). Most environments will keep this false because static-asset delivery via CloudFront is cheaper and faster than serving via Fargate. Per AAP Sec 0.7.7 the two-Docker-image release strategy supports either pattern."
  type        = bool
  default     = false
}

variable "frontend_image_uri" {
  description = "Full ECR URI for the frontend nginx container image. Required (must be non-empty) when deploy_frontend_container = true; ignored otherwise. The cross-variable requirement is enforced via lifecycle.precondition on aws_ecs_service.frontend in main.tf since Terraform 1.7 does not support cross-variable validation."
  type        = string
  default     = ""
}

variable "frontend_backend_api_base_url" {
  description = "Optional runtime API base URL injected into the frontend container as the BACKEND_API_BASE_URL environment variable. Only meaningful if the frontend image supports runtime config injection (post-MVP feature). When empty (default), the frontend uses build-time configuration baked into the static assets. Ignored when deploy_frontend_container = false."
  type        = string
  default     = ""
}

###############################################################################
# 4. Container ports
###############################################################################

variable "backend_container_port" {
  description = "TCP port where the backend Gunicorn process listens inside the container. Default 8000 matches the Gunicorn binding convention in backend/Dockerfile (gunicorn --bind 0.0.0.0:8000). The tasks security group ingress rule and the ALB target group's port both reference this value."
  type        = number
  default     = 8000

  validation {
    condition     = var.backend_container_port >= 1 && var.backend_container_port <= 65535
    error_message = "backend_container_port must be between 1 and 65535 (valid TCP port range)."
  }
}

variable "frontend_container_port" {
  description = "TCP port where the frontend nginx serves static assets inside the container. Default 80 matches the standard nginx config (frontend/nginx.conf listens on 80). Ignored when deploy_frontend_container = false."
  type        = number
  default     = 80

  validation {
    condition     = var.frontend_container_port >= 1 && var.frontend_container_port <= 65535
    error_message = "frontend_container_port must be between 1 and 65535 (valid TCP port range)."
  }
}

###############################################################################
# 5. Compute sizing (Fargate quantization rules apply)
#
# AWS Fargate enforces a discrete CPU enum and a CPU-memory compatibility
# matrix. This module validates task_cpu against the discrete enum and
# task_memory against the broad supported range; the cross-product check
# (e.g., 512 CPU only supports 1024-4096 MiB memory) is enforced at apply
# time by the AWS API since Terraform 1.7 does not support cross-variable
# validation in variable blocks.
###############################################################################

variable "task_cpu" {
  description = "Fargate task CPU units. Must be a valid Fargate quantum: 256 (0.25 vCPU), 512 (0.5 vCPU), 1024 (1 vCPU), 2048 (2 vCPU), 4096 (4 vCPU), 8192 (8 vCPU), 16384 (16 vCPU). Default 512 is appropriate for MVP scale (10K records per org). Bump to 1024 or higher under heavier load."
  type        = number
  default     = 512

  validation {
    condition     = contains([256, 512, 1024, 2048, 4096, 8192, 16384], var.task_cpu)
    error_message = "task_cpu must be one of the Fargate-supported values: 256, 512, 1024, 2048, 4096, 8192, 16384."
  }
}

variable "task_memory" {
  description = "Fargate task memory in MiB. Must be compatible with task_cpu per AWS's CPU-memory matrix (e.g., 256 CPU supports 512-2048 MiB; 512 CPU supports 1024-4096 MiB; 1024 CPU supports 2048-8192 MiB). Default 1024 MiB pairs with default task_cpu = 512. Compatibility is enforced at apply time by the AWS API."
  type        = number
  default     = 1024

  validation {
    condition     = var.task_memory >= 512 && var.task_memory <= 122880
    error_message = "task_memory must be between 512 MiB and 120 GiB (122880 MiB) - the broad Fargate range. Note: must additionally be compatible with task_cpu per the Fargate CPU-memory matrix; AWS rejects invalid combinations at apply time."
  }
}

variable "desired_count" {
  description = "Number of backend service task replicas to run. Default 2 for AZ redundancy (one task per AZ across the two AZs declared in private_subnet_ids). Note: the aws_ecs_service.backend resource sets lifecycle { ignore_changes = [desired_count] } so autoscaling can adjust this at runtime without producing Terraform drift."
  type        = number
  default     = 2

  validation {
    condition     = var.desired_count >= 0 && var.desired_count <= 100
    error_message = "desired_count must be between 0 and 100. Use 0 only for temporary scale-to-zero scenarios (e.g., pausing dev environments overnight); production should be at least 2 for AZ redundancy."
  }
}

###############################################################################
# 6. Operational toggles
###############################################################################

variable "enable_execute_command" {
  description = "When true, enable ECS Exec (SSM-based shell access into running tasks for debugging via 'aws ecs execute-command'). MUST default to false per the folder-spec critical constraint and AAP Sec 0.7.4 security invariant - shell access in production is a privileged escalation that requires explicit operator opt-in. Setting this to true also requires the SSM-related IAM grants on the task role (handled in iam.tf when this flag is true)."
  type        = bool
  default     = false
}

variable "secrets_kms_key_id" {
  description = "Optional ARN or key ID of a customer-managed KMS CMK used to encrypt the four application secrets in Secrets Manager. When non-empty, the execution role's IAM policy gets a kms:Decrypt grant scoped to this key. When empty (default), secrets use the AWS-managed 'aws/secretsmanager' key (no extra IAM grant needed; Secrets Manager performs the decrypt transparently). Empty-string convention follows the sibling secrets module."
  type        = string
  default     = ""
}


###############################################################################
# 7. Secret ARNs (consumed from modules/secrets/)
#
# CRITICAL (AAP Sec 0.7.4 security invariant): The IAM policies for both task
# and execution roles scope secretsmanager:GetSecretValue to ONLY these four
# ARNs - never wildcards. Any new secret added in modules/secrets/ requires a
# new variable here and a corresponding entry in
# data.aws_iam_policy_document.task_secrets_read and
# execution_secrets_resolve in iam.tf / data.tf.
#
# The ARNs themselves are NOT sensitive (they are identifiers, not values);
# the secret VALUES are resolved by the ECS agent at task start via the
# execution role's IAM grant on these specific ARNs.
###############################################################################

variable "secret_anthropic_api_key_arn" {
  description = "ARN of the Secrets Manager secret holding ANTHROPIC_API_KEY. Injected as the env var ANTHROPIC_API_KEY at task start via the ECS native secrets block (the value never appears in the task definition JSON; only the ARN does). Sourced from module.secrets.anthropic_api_key_arn in the root composition."
  type        = string

  validation {
    condition     = can(regex("^arn:[a-z0-9-]+:secretsmanager:[a-z0-9-]+:[0-9]+:secret:", var.secret_anthropic_api_key_arn))
    error_message = "secret_anthropic_api_key_arn must be a valid Secrets Manager secret ARN matching 'arn:<partition>:secretsmanager:<region>:<account>:secret:<name>'."
  }
}

variable "secret_google_oauth_client_secret_arn" {
  description = "ARN of the Secrets Manager secret holding GOOGLE_OAUTH_CLIENT_SECRET. Injected as the env var GOOGLE_OAUTH_CLIENT_SECRET at task start via the ECS native secrets block. Sourced from module.secrets.google_oauth_client_secret_arn in the root composition."
  type        = string

  validation {
    condition     = can(regex("^arn:[a-z0-9-]+:secretsmanager:[a-z0-9-]+:[0-9]+:secret:", var.secret_google_oauth_client_secret_arn))
    error_message = "secret_google_oauth_client_secret_arn must be a valid Secrets Manager secret ARN matching 'arn:<partition>:secretsmanager:<region>:<account>:secret:<name>'."
  }
}

variable "secret_jwt_signing_key_arn" {
  description = "ARN of the Secrets Manager secret holding JWT_SIGNING_KEY (HS256 symmetric key for session JWTs minted by backend/app/services/auth.py). Injected as the env var JWT_SIGNING_KEY at task start via the ECS native secrets block. Sourced from module.secrets.jwt_signing_key_arn in the root composition."
  type        = string

  validation {
    condition     = can(regex("^arn:[a-z0-9-]+:secretsmanager:[a-z0-9-]+:[0-9]+:secret:", var.secret_jwt_signing_key_arn))
    error_message = "secret_jwt_signing_key_arn must be a valid Secrets Manager secret ARN matching 'arn:<partition>:secretsmanager:<region>:<account>:secret:<name>'."
  }
}

variable "secret_db_password_arn" {
  description = "ARN of the Secrets Manager secret holding the PostgreSQL master password. Injected as the env var DB_PASSWORD at task start via the ECS native secrets block. The backend constructs the SQLAlchemy DSN at process startup from DB_HOST, DB_PORT, DB_NAME, DB_USERNAME, DB_PASSWORD - keeping the password out of the task definition JSON entirely. Sourced from module.secrets.db_password_arn in the root composition."
  type        = string

  validation {
    condition     = can(regex("^arn:[a-z0-9-]+:secretsmanager:[a-z0-9-]+:[0-9]+:secret:", var.secret_db_password_arn))
    error_message = "secret_db_password_arn must be a valid Secrets Manager secret ARN matching 'arn:<partition>:secretsmanager:<region>:<account>:secret:<name>'."
  }
}

###############################################################################
# 8. Database connection (non-secret components only)
#
# DB_PASSWORD is NOT declared here - it comes from secret_db_password_arn
# (above) and is injected at task start via the ECS native secrets block.
# These four values are non-secret connection metadata that the backend uses
# to construct the SQLAlchemy DSN.
###############################################################################

variable "db_host" {
  description = "RDS endpoint hostname (without port). Output of module.database.endpoint in the root composition. Used by backend/app/config.py to construct the SQLAlchemy DSN at process startup."
  type        = string

  validation {
    condition     = length(var.db_host) > 0
    error_message = "db_host must be non-empty (typically the RDS writer endpoint, e.g., 'sales-connections-prod-postgres.xxxxx.us-east-1.rds.amazonaws.com')."
  }
}

variable "db_port" {
  description = "RDS port number. Default 5432 for PostgreSQL. Override only if the RDS parameter group has been customized to a non-standard port (rare)."
  type        = number
  default     = 5432

  validation {
    condition     = var.db_port >= 1 && var.db_port <= 65535
    error_message = "db_port must be between 1 and 65535 (valid TCP port range)."
  }
}

variable "db_name" {
  description = "Database name within the RDS instance. Output of module.database.database_name in the root composition. Default value at the database module is 'sales_connections'."
  type        = string

  validation {
    condition     = length(var.db_name) > 0
    error_message = "db_name must be non-empty (PostgreSQL database identifier, typically 'sales_connections')."
  }
}

variable "db_username" {
  description = "Database username. Output of module.database.master_username in the root composition. Default value at the database module is 'sales_connections_app'. The corresponding password is supplied separately via secret_db_password_arn (see section 7) and never appears in this variables file."
  type        = string

  validation {
    condition     = length(var.db_username) > 0
    error_message = "db_username must be non-empty (PostgreSQL role identifier, typically 'sales_connections_app')."
  }
}

###############################################################################
# 9. Backend application configuration (env vars per AAP Sec 0.4)
#
# These are NON-SECRET config values - they appear in the task definition
# JSON in cleartext (visible in the AWS console and CloudTrail). Anything
# sensitive must use the secrets block (section 7), not the environment
# block.
###############################################################################

variable "flask_env" {
  description = "Value of the FLASK_ENV env var. Maps to backend/app/config.py config class selection (DevelopmentConfig / TestingConfig / ProductionConfig). Defaults to 'production'. CRITICAL: Setting this to 'development' in production would enable Flask's debugger and other dev-only behaviors that leak information; operators must NEVER set this to 'development' outside local Docker Compose."
  type        = string
  default     = "production"

  validation {
    condition     = contains(["development", "testing", "production"], var.flask_env)
    error_message = "flask_env must be one of: development, testing, production."
  }
}

variable "log_level" {
  description = "Value of the LOG_LEVEL env var consumed by structlog (configured in backend/app/observability/logging.py). Defaults to 'INFO'. Use 'DEBUG' temporarily during incident investigation; revert to 'INFO' or higher in steady state to control log volume / cost."
  type        = string
  default     = "INFO"

  validation {
    condition     = contains(["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"], var.log_level)
    error_message = "log_level must be one of: DEBUG, INFO, WARNING, ERROR, CRITICAL (Python logging standard levels)."
  }
}

variable "log_format" {
  description = "Value of the LOG_FORMAT env var consumed by structlog. 'json' produces structured JSON suitable for CloudWatch Logs Insights queries (production default); 'console' produces human-readable colored output suitable for local docker-compose."
  type        = string
  default     = "json"

  validation {
    condition     = contains(["json", "console"], var.log_format)
    error_message = "log_format must be one of: json, console."
  }
}

variable "cors_allowed_origins" {
  description = "Comma-separated list of allowed CORS origins (e.g., 'https://app.example.com,https://staging.example.com'). Empty string (default) disables CORS - the SPA and API share the same origin via the ALB so CORS is unnecessary in the standard deployment topology. Used by Flask-CORS configuration in backend/app/__init__.py."
  type        = string
  default     = ""
}

variable "default_org_id" {
  description = "UUID (or equivalent string identifier) of the single organization served in MVP. Backend uses this as the implicit org_id for new users when their email domain doesn't otherwise resolve to an org. Per AAP Sec 1.3, MVP runs single-org even though the data model is multi-tenant (every entity carries an org_id column for forward compatibility). Required input - no sensible default."
  type        = string

  validation {
    condition     = length(var.default_org_id) > 0
    error_message = "default_org_id must be non-empty (typically a UUID, e.g., '00000000-0000-0000-0000-000000000001' for the seed org)."
  }
}

variable "default_new_user_role" {
  description = "Default role assigned to newly created users (e.g., first-time Google OAuth login when no manual role assignment has occurred yet). Per AAP Sec 0.7.6, contributors can submit records but cannot mutate outreach status; sales reps (Viewer role) can mutate outreach status but cannot moderate; Admins can do everything including hard-delete. Defaults to 'Contributor' as the least-privilege option per AAP Sec 0.5.2."
  type        = string
  default     = "Contributor"

  validation {
    condition     = contains(["Admin", "Contributor", "Viewer"], var.default_new_user_role)
    error_message = "default_new_user_role must be one of: Admin, Contributor, Viewer (the three roles defined in backend/app/models/enums.py UserRole)."
  }
}

variable "anthropic_model" {
  description = "Anthropic Claude model identifier passed through to Langchain's ChatAnthropic constructor (in backend/app/services/ai_orchestration.py). Default 'claude-sonnet-4-5' balances output quality against latency for the F-002 5-second P95 budget. Operators may switch to 'claude-opus-4-1' for higher-quality outputs at higher latency; record the change in docs/decision-log.md per the Explainability rule."
  type        = string
  default     = "claude-sonnet-4-5"

  validation {
    condition     = length(var.anthropic_model) > 0
    error_message = "anthropic_model must be non-empty (e.g., 'claude-sonnet-4-5', 'claude-opus-4-1')."
  }
}

variable "ai_request_timeout_seconds" {
  description = "Per-request Anthropic API timeout in seconds. Default 5 matches the AAP Sec 0.7.3 P95 latency budget for AI note generation exactly. The application enforces this via a watchdog in backend/app/services/ai_orchestration.py; on timeout the handler returns HTTP 504 with error.code = 'ai_timeout' so the SPA can show the non-blocking 'AI unavailable; you can still submit' affordance. Tuning lower could trip the SLO; tuning higher could exceed the budget - both directions need an explicit decision-log entry."
  type        = number
  default     = 5

  validation {
    condition     = var.ai_request_timeout_seconds >= 1 && var.ai_request_timeout_seconds <= 60
    error_message = "ai_request_timeout_seconds must be between 1 and 60 seconds (1 = aggressive minimum; 60 = generous ceiling well above the 5 s P95 budget)."
  }
}

variable "google_oauth_redirect_uri" {
  description = "OAuth 2.0 redirect URI registered with the Google application (must match exactly what is configured in the Google Cloud Console OAuth client). Typically 'https://<alb-dns-or-domain>/auth/google/callback'. The validation here accepts both http:// and https:// to support local development; production must use https:// (constraint documented in docs/security.md, not enforced here, since the variable is environment-agnostic)."
  type        = string

  validation {
    condition     = length(var.google_oauth_redirect_uri) > 0
    error_message = "google_oauth_redirect_uri must be non-empty."
  }

  validation {
    condition     = can(regex("^https?://", var.google_oauth_redirect_uri))
    error_message = "google_oauth_redirect_uri must start with http:// or https:// (production should always be https:// per AAP Sec 0.7.4 security invariants)."
  }
}

###############################################################################
# 10. Observability (consumed from modules/observability/)
###############################################################################

variable "cloudwatch_log_group_name" {
  description = "CloudWatch Logs group name where backend (and optionally frontend) container stdout/stderr is streamed via the awslogs Docker driver. Output of module.observability.log_group_name in the root composition. The IAM policies in iam.tf scope log-write permissions to THIS group only (least privilege - never wildcard)."
  type        = string

  validation {
    condition     = length(var.cloudwatch_log_group_name) > 0
    error_message = "cloudwatch_log_group_name must be non-empty (e.g., '/aws/ecs/sales-connections-prod')."
  }
}

variable "otlp_exporter_endpoint" {
  description = "OTLP exporter endpoint (HTTP) for distributed tracing exports from backend/app/observability/tracing.py. Empty string (default) disables OTLP export - useful for development and for environments not yet integrated with X-Ray. For AWS X-Ray integration, set to the ADOT (AWS Distro for OpenTelemetry) collector's OTLP HTTP receiver URL, typically 'http://localhost:4318' when the collector runs as a sidecar."
  type        = string
  default     = ""
}

variable "otel_service_name" {
  description = "OpenTelemetry service name attached to all spans emitted by the backend. Default 'sales-connections-api' per AAP Sec 0.5.2 - keep this stable across environments so traces aggregate correctly in the observability backend. Override only if running multiple distinct backend services in the same OTLP collector."
  type        = string
  default     = "sales-connections-api"

  validation {
    condition     = length(var.otel_service_name) > 0
    error_message = "otel_service_name must be non-empty."
  }
}

###############################################################################
# 11. Health check
###############################################################################

variable "health_check_path" {
  description = "HTTP path probed by the container-level health check (running INSIDE the container via the HEALTHCHECK directive in backend/Dockerfile). Distinct from the ALB target-group health check (which is configured in modules/alb/). Default '/healthz' - the unconditional liveness probe per AAP Sec 0.5.2 (returns 200 unconditionally without DB round-trip). Use '/readyz' only if the container check should also verify DB connectivity (NOT recommended - liveness should not flap on transient DB hiccups)."
  type        = string
  default     = "/healthz"

  validation {
    condition     = can(regex("^/", var.health_check_path))
    error_message = "health_check_path must start with / (HTTP path component)."
  }
}

