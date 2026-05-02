###############################################################################
# infra/terraform/modules/ecs/locals.tf
#
# Module-internal locals for the ECS module:
#
#   1. module_tags                       Standard tag set merged into every
#                                        resource (Component=ECS, Environment,
#                                        ManagedBy, Module).
#
#   2. Resource naming locals            cluster_name, backend_service_name,
#                                        frontend_service_name,
#                                        backend_task_family,
#                                        frontend_task_family,
#                                        security_group_name_prefix,
#                                        task_role_name, execution_role_name
#                                        (all derived from var.name_prefix).
#
#   3. Backend container definition assembly:
#        - backend_environment           Non-secret env vars (16 entries).
#        - backend_secrets               Secrets Manager ARN injection (4).
#        - backend_log_configuration     awslogs Docker driver config.
#        - backend_health_check          Python stdlib check (no curl).
#        - backend_container_definitions Full container map (list of 1).
#
#   4. Frontend container definition assembly (count-gated by
#      var.deploy_frontend_container in main.tf):
#        - frontend_environment          BACKEND_API_BASE_URL only.
#        - frontend_log_configuration    awslogs driver config.
#        - frontend_health_check         BusyBox wget check (nginx image).
#        - frontend_container_definitions Full container map (list of 1).
#
#   5. Derived flags                     create_frontend, create_kms_policy.
#                                        Centralized count-gate expressions.
#
# Multiple locals { ... } blocks are used for organization; Terraform merges
# them within a single module at parse time. Locals here are pure functions
# of input variables and the aws_region data source, so terraform plan
# evaluates them at parse time without an AWS API call.
###############################################################################

###############################################################################
# Standard module tags
#
# Merged into every taggable resource in this module. Operators can extend
# via var.tags. Module-internal tags (Component, Environment, ManagedBy,
# Module) cannot be overridden via var.tags by accident because merge()
# right-most-wins: the static map below wins over var.tags.
###############################################################################

locals {
  module_tags = merge(
    var.tags,
    {
      Component   = "ECS"
      Environment = var.environment
      ManagedBy   = "Terraform"
      Module      = "infra/terraform/modules/ecs"
    },
  )
}

###############################################################################
# Resource naming locals
#
# All ECS resources share var.name_prefix as the leading token. Defining
# the per-resource prefixes here keeps the naming consistent and simplifies
# rename operations (single-line edits centralize the change).
#
# AWS naming constraints honored:
#   - aws_ecs_cluster.name:          max 255 chars, alphanumeric, hyphens,
#                                    underscores. "<prefix>-cluster" fits
#                                    well within bounds.
#   - aws_ecs_service.name:          max 255 chars, same charset.
#                                    "<prefix>-backend-service" fits.
#   - aws_ecs_task_definition.family: max 255 chars, same charset.
#                                    "<prefix>-backend" fits.
#   - aws_iam_role.name:             max 64 chars, alphanumeric, +=,.@-_.
#                                    With var.name_prefix capped at 30 chars
#                                    in variables.tf, "<prefix>-task-role"
#                                    (10-char suffix) and
#                                    "<prefix>-execution-role" (15-char
#                                    suffix) both fit safely.
#   - aws_security_group.name_prefix: AWS appends a random hex suffix, so
#                                    the trailing hyphen produces
#                                    "<prefix>-tasks-<random>" rather than
#                                    "<prefix>-tasks<random>". Security
#                                    group names can be up to 255 chars,
#                                    so prefix length is not binding here.
###############################################################################

locals {
  cluster_name               = "${var.name_prefix}-cluster"
  backend_service_name       = "${var.name_prefix}-backend-service"
  frontend_service_name      = "${var.name_prefix}-frontend-service"
  backend_task_family        = "${var.name_prefix}-backend"
  frontend_task_family       = "${var.name_prefix}-frontend"
  security_group_name_prefix = "${var.name_prefix}-tasks-"
  task_role_name             = "${var.name_prefix}-task-role"
  execution_role_name        = "${var.name_prefix}-execution-role"
}

###############################################################################
# Backend container - non-secret environment variables
#
# These env vars are visible in the task definition (and therefore in
# CloudTrail / the AWS console). Secret values use the `secrets` block
# below (which references Secrets Manager ARNs and is resolved by ECS at
# task start without ever exposing the values to console or logs).
#
# Per AAP Sec 0.4.4 the backend reads ANTHROPIC_API_KEY from Secrets
# Manager at process startup; that env var is injected by ECS via the
# `secrets` block. Per AAP Sec 0.5.2 the backend reads non-secret config
# from env vars directly (FLASK_ENV, LOG_LEVEL, etc.).
#
# Numeric values are wrapped in tostring(...) because ECS container
# definitions specify env-var values as strings - passing a number
# directly into a `value` field would fail Terraform validation.
#
# AWS_REGION is set explicitly so boto3 SDK clients in the backend skip
# the IMDS round-trip on default region resolution.
#
# DATABASE_URL is intentionally NOT included - the application constructs
# the SQLAlchemy DSN at startup from DB_HOST, DB_PORT, DB_NAME, DB_USERNAME
# and DB_PASSWORD (the password coming from the `secrets` block). This
# separation lets operators swap DB hosts (e.g., for blue-green DB cutover)
# without exposing credentials in any cleartext field.
###############################################################################

locals {
  backend_environment = [
    { name = "FLASK_ENV", value = var.flask_env },
    { name = "LOG_LEVEL", value = var.log_level },
    { name = "LOG_FORMAT", value = var.log_format },
    { name = "CORS_ALLOWED_ORIGINS", value = var.cors_allowed_origins },
    { name = "DEFAULT_ORG_ID", value = var.default_org_id },
    { name = "DEFAULT_NEW_USER_ROLE", value = var.default_new_user_role },
    { name = "ANTHROPIC_MODEL", value = var.anthropic_model },
    { name = "AI_REQUEST_TIMEOUT_SECONDS", value = tostring(var.ai_request_timeout_seconds) },
    { name = "GOOGLE_OAUTH_REDIRECT_URI", value = var.google_oauth_redirect_uri },
    { name = "DB_HOST", value = var.db_host },
    { name = "DB_PORT", value = tostring(var.db_port) },
    { name = "DB_NAME", value = var.db_name },
    { name = "DB_USERNAME", value = var.db_username },
    { name = "OTLP_EXPORTER_ENDPOINT", value = var.otlp_exporter_endpoint },
    { name = "OTEL_SERVICE_NAME", value = var.otel_service_name },
    { name = "AWS_REGION", value = data.aws_region.current.name },
  ]
}

###############################################################################
# Backend container - secrets injected from AWS Secrets Manager
#
# ECS resolves these `valueFrom` ARNs at task start (using the execution
# role's secretsmanager:GetSecretValue permission, granted in iam.tf). The
# resolved plaintext values are injected as env vars; they NEVER appear in
# the task definition JSON, never in the AWS console, never in CloudTrail.
#
# CRITICAL (AAP Sec 0.7.4): plaintext secret values are NEVER in the task
# definition. Only ARNs.
#
# Mapping:
#   ANTHROPIC_API_KEY          -> secret_anthropic_api_key_arn
#   GOOGLE_OAUTH_CLIENT_SECRET -> secret_google_oauth_client_secret_arn
#   JWT_SIGNING_KEY            -> secret_jwt_signing_key_arn
#   DB_PASSWORD                -> secret_db_password_arn
#
# These four env vars are then read by backend/app/config.py at startup.
###############################################################################

locals {
  backend_secrets = [
    { name = "ANTHROPIC_API_KEY", valueFrom = var.secret_anthropic_api_key_arn },
    { name = "GOOGLE_OAUTH_CLIENT_SECRET", valueFrom = var.secret_google_oauth_client_secret_arn },
    { name = "JWT_SIGNING_KEY", valueFrom = var.secret_jwt_signing_key_arn },
    { name = "DB_PASSWORD", valueFrom = var.secret_db_password_arn },
  ]
}

###############################################################################
# Backend container - awslogs driver configuration
#
# The awslogs Docker driver streams stdout / stderr to CloudWatch Logs.
# The log group is provisioned by modules/observability/ and passed in via
# var.cloudwatch_log_group_name. Stream names are prefixed "backend" so
# operators can distinguish backend logs from frontend logs in the same
# log group when the optional frontend container is also deployed.
#
# awslogs-region is sourced from data.aws_region.current.name (NOT a
# hardcoded region or var) so the module is portable across regions
# without modification.
###############################################################################

locals {
  backend_log_configuration = {
    logDriver = "awslogs"
    options = {
      "awslogs-group"         = var.cloudwatch_log_group_name
      "awslogs-region"        = data.aws_region.current.name
      "awslogs-stream-prefix" = "backend"
    }
  }
}

###############################################################################
# Backend container - health check command
#
# CRITICAL: the check uses Python stdlib (urllib.request) to avoid
# requiring `curl` in the python:3.12-slim base image. Per the folder spec
# critical constraint: "Health check via Python - Avoids requiring curl
# in the slim base image; uses stdlib urllib.request".
#
# Hits var.health_check_path (default /healthz) on localhost. Per AAP
# Sec 0.5.2, /healthz is the unconditional liveness probe (returns 200
# always); /readyz is the readiness probe with DB ping (NOT used here
# because the liveness check should be cheap and not flap on transient
# DB hiccups).
#
# Timing parameters:
#   - interval:    30s   (probe every 30 seconds)
#   - timeout:     5s    (each probe waits up to 5s for response)
#   - retries:     3     (3 consecutive failures = unhealthy)
#   - startPeriod: 30s   (grace period during cold start)
#
# Note on the urllib timeout=3 inside the Python expression: this is the
# per-request socket timeout, ensuring the probe self-terminates within
# the 5s docker health-check timeout (3s socket + 2s Python interpreter
# overhead = comfortably under 5s).
###############################################################################

locals {
  backend_health_check = {
    command = [
      "CMD-SHELL",
      "python -c 'import urllib.request; urllib.request.urlopen(\"http://localhost:${var.backend_container_port}${var.health_check_path}\", timeout=3).read()'",
    ]
    interval    = 30
    timeout     = 5
    retries     = 3
    startPeriod = 30
  }
}

###############################################################################
# Backend container definition (composed)
#
# Assembles the per-container map that will be passed to
# aws_ecs_task_definition.backend.container_definitions via jsonencode().
#
# Defining as a structured local (rather than a JSON string) keeps the
# Terraform diff output readable and lets the AWS provider validate the
# shape at plan time.
#
# Key per-field rationale:
#   - cpu = 0: At the per-container level, 0 means "share whatever the
#     task-level CPU permits". Only matters in multi-container tasks
#     where strict per-container quotas are desired; both backend and
#     frontend are single-container task definitions, so 0 is correct.
#   - readonlyRootFilesystem = false: Python packages (notably tempfile
#     and certain SDK packages) write to /tmp at runtime. Setting true
#     would require additional mount points; deferred as a hardening
#     enhancement post-MVP.
#   - stopTimeout = 30: Matches the SIGTERM grace period configured on
#     Gunicorn in backend/Dockerfile. On task replacement or scale-in,
#     ECS sends SIGTERM and waits up to 30s before SIGKILL. Aligning
#     prevents in-flight requests from being dropped during deployments.
#   - hostPort = containerPort: Required for awsvpc network mode (the
#     network mode used by Fargate); the two values must match.
###############################################################################

locals {
  backend_container_definitions = [
    {
      name      = "backend"
      image     = var.backend_image_uri
      essential = true
      cpu       = 0
      portMappings = [
        {
          containerPort = var.backend_container_port
          hostPort      = var.backend_container_port
          protocol      = "tcp"
        },
      ]
      environment            = local.backend_environment
      secrets                = local.backend_secrets
      logConfiguration       = local.backend_log_configuration
      healthCheck            = local.backend_health_check
      readonlyRootFilesystem = false
      stopTimeout            = 30
    },
  ]
}

###############################################################################
# Frontend container definition (composed) - OPTIONAL
#
# When var.deploy_frontend_container is true, these locals provide the
# container definition for the frontend nginx static-asset image. The
# frontend has NO secrets (it's served as static files; all auth happens
# through the SPA's calls to the backend API).
#
# When var.deploy_frontend_container is false (the default), the frontend
# is served via S3 + CloudFront (a future deployment topology) and the
# corresponding ECS service / task definition are not created at all.
#
# These locals are unconditionally defined (Terraform requires
# deterministic shape) but consumed only when local.create_frontend is
# true via count-gating on the resources in main.tf.
#
# Frontend health check uses BusyBox `wget` (not Python) because nginx
# images typically include `wget` but not Python. The frontend nginx
# image is a different base image from the Python backend.
#
# The startPeriod for the frontend (15s) is shorter than the backend's
# (30s) because nginx starts dramatically faster than a Python WSGI
# worker pool.
###############################################################################

locals {
  frontend_environment = [
    { name = "BACKEND_API_BASE_URL", value = var.frontend_backend_api_base_url },
  ]

  frontend_log_configuration = {
    logDriver = "awslogs"
    options = {
      "awslogs-group"         = var.cloudwatch_log_group_name
      "awslogs-region"        = data.aws_region.current.name
      "awslogs-stream-prefix" = "frontend"
    }
  }

  frontend_health_check = {
    command = [
      "CMD-SHELL",
      "wget -q -O - http://localhost:${var.frontend_container_port}/ >/dev/null || exit 1",
    ]
    interval    = 30
    timeout     = 5
    retries     = 3
    startPeriod = 15
  }

  frontend_container_definitions = [
    {
      name      = "frontend"
      image     = var.frontend_image_uri
      essential = true
      cpu       = 0
      portMappings = [
        {
          containerPort = var.frontend_container_port
          hostPort      = var.frontend_container_port
          protocol      = "tcp"
        },
      ]
      environment            = local.frontend_environment
      secrets                = []
      logConfiguration       = local.frontend_log_configuration
      healthCheck            = local.frontend_health_check
      readonlyRootFilesystem = false
      stopTimeout            = 30
    },
  ]
}

###############################################################################
# Derived flags for resource gating
#
# Centralizes count-gate expressions so they're consistent across the
# module. Resources in main.tf and iam.tf reference these locals via
# count = local.create_X ? 1 : 0 patterns rather than re-evaluating the
# underlying variable expressions, which keeps the gating logic in one
# place and easy to audit.
#
# create_frontend:
#   True when var.deploy_frontend_container is true. Drives the count of
#   aws_ecs_task_definition.frontend, aws_ecs_service.frontend, and any
#   frontend-only auxiliary resources in main.tf.
#
# create_kms_policy:
#   True when var.secrets_kms_key_id is non-empty (operator opted into a
#   customer-managed CMK for the four application secrets). Drives the
#   count of aws_iam_role_policy.execution_kms in iam.tf and mirrors the
#   count gating already applied to data.aws_iam_policy_document.execution_kms
#   in data.tf.
###############################################################################

locals {
  create_frontend   = var.deploy_frontend_container
  create_kms_policy = var.secrets_kms_key_id != ""
}
