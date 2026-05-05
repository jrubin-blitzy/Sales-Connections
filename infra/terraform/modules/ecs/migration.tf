###############################################################################
# infra/terraform/modules/ecs/migration.tf
#
# One-shot Alembic migration ECS task definition for the Sales-Connections
# platform.
#
# Per AAP Sec 0.5.2 (Layer 0) and docs/operations.md "Database migrations":
#
#   1. Migrations live in backend/migrations/versions/ and are applied via
#      `alembic upgrade head`.
#
#   2. In production, the long-running backend tasks run with
#      RUN_MIGRATIONS=false (default in backend/Dockerfile). The schema
#      migration is performed by a separate one-shot ECS RunTask invocation
#      with RUN_MIGRATIONS=true and the new image SHA, scheduled BEFORE the
#      rolling deployment of the long-running tasks.
#
#   3. Migrations are forward-compatible per the migration policy in
#      docs/decision-log.md (see DL-0043 for the 0001/0002 migration split
#      and the audit_events GRANT/REVOKE invariant), so the previous
#      application version continues serving traffic against the new schema
#      during the rolling swap.
#
# QA Final Checkpoint 13 review (Issue #4, MAJOR) flagged that the documented
# `sales-connections-<env>-migration` task definition referenced in
# docs/operations.md did not exist in any Terraform module, leaving the
# production deployment workflow with a documented but non-functional
# migration step. This file closes that gap.
#
# Resources defined:
#
#   1. aws_ecs_task_definition.migration         One-shot Alembic task def
#                                                that the CD pipeline (or
#                                                an operator) invokes via
#                                                `aws ecs run-task` with
#                                                RUN_MIGRATIONS=true.
#
# Reuses (does NOT duplicate):
#   - The same backend container image (var.backend_image_uri)
#   - The same task and execution roles (aws_iam_role.task / .execution)
#     because both roles already have the secretsmanager:GetSecretValue
#     grant on the four application secret ARNs and the cloudwatch logs
#     write grant on the shared log group. The migration container needs
#     ALL of these:
#       - DB_PASSWORD     -> to authenticate against RDS for ALTER/CREATE/etc.
#       - JWT_SIGNING_KEY -> the app config loader requires it to instantiate
#                            even though the migration codepath never mints
#                            a JWT.
#       - ANTHROPIC_API_KEY / GOOGLE_OAUTH_CLIENT_SECRET -> the app config
#                            loader requires both to instantiate the Flask
#                            app object that Alembic env.py imports.
#     Reusing the existing roles avoids duplicating four IAM grants and
#     keeps the security-sensitive ARN allowlist in one place (per AAP
#     Sec 0.7.4 invariant: secretsmanager:GetSecretValue is scoped to
#     EXACTLY the four secret ARNs).
#
# Critical differences vs the long-running backend task definition:
#   - family   = "<name_prefix>-migration"  (e.g., sales-connections-prod-migration)
#               This MATCHES the task-definition name documented in
#               docs/operations.md (lines 81-91) and `aws ecs run-task
#               --task-definition sales-connections-prod-migration`.
#   - container.command overrides the Dockerfile CMD with a direct
#               `alembic upgrade head` invocation. The container exits 0
#               on success and non-zero on failure - the desired one-shot
#               semantics for `aws ecs run-task --launch-type FARGATE`.
#   - container.essential = true (the same default as the backend) so a
#               crash exits the task with a non-zero exit code, which the
#               CD workflow's `aws ecs run-task --wait tasks-stopped` plus
#               exit-code inspection treats as a deployment failure.
#   - container.healthCheck OMITTED  Migration tasks are short-lived
#               (typically 5-30 seconds for additive migrations) and
#               health checks are designed for long-running workloads.
#   - container.portMappings OMITTED  Migrations do not listen on any
#               port; the container exits as soon as alembic completes.
#   - The aws_ecs_service resource pattern is NOT used.  Migrations are
#               invoked via `aws ecs run-task`, which is the canonical
#               one-shot ECS Fargate invocation pattern.
#
# Build order dependencies (per AAP Sec 0.5.2):
#   - This file depends on iam.tf for the task and execution role ARNs.
#   - This file depends on locals.tf for local.module_tags and for the
#     secrets/log-config locals it reuses verbatim.
#   - data.aws_region.current is sourced from data.tf (already declared).
#
# Ignored Terraform drift surfaces:
#   - lifecycle { ignore_changes = [container_definitions] } so the CD
#     pipeline can register new revisions (with bumped image tags) via
#     `aws ecs register-task-definition` without Terraform reverting them.
#     Mirrors the same convention applied to the long-running backend
#     and frontend services in main.tf.
###############################################################################

###############################################################################
# Migration task - non-secret environment variables
#
# Mirrors the long-running backend container's environment block but adds
# RUN_MIGRATIONS=true so backend/Dockerfile's preflight branch fires:
#
#     CMD ["sh", "-c", "if [ \"$RUN_MIGRATIONS\" = \"true\" ]; then echo
#                       'Running Alembic migrations...'; alembic upgrade
#                       head; fi && exec gunicorn -c /app/gunicorn.conf.py
#                       wsgi:app ..."]
#
# Both env-var sets are identical save for the RUN_MIGRATIONS flag and the
# absence of gunicorn-specific tunables (no PORT etc. needed). Reusing the
# backend_environment local would still work because the unused vars are
# benign at migration time, but copying via concat() makes the override
# of RUN_MIGRATIONS explicit.
#
# The migration container does NOT use the gunicorn launcher; the command
# override below replaces the Dockerfile CMD entirely with a direct
# `alembic upgrade head` invocation. The RUN_MIGRATIONS env var is set
# anyway as a defense-in-depth signal: if the command override is later
# changed back to the default CMD, the env var ensures migrations still
# fire and the workload still self-terminates (gunicorn would never start
# because the migration would have completed first; the container would
# only stop when the gunicorn process exited, but operators inspecting
# the running task definition would see RUN_MIGRATIONS=true as a clear
# signal that this is a one-shot task definition, not a long-running one).
###############################################################################

locals {
  migration_environment = concat(
    local.backend_environment,
    [
      { name = "RUN_MIGRATIONS", value = "true" },
    ],
  )
}

###############################################################################
# Migration task - log configuration
#
# Streams Alembic stdout/stderr to the same CloudWatch log group as the
# backend, but with a distinct stream prefix ("migration") so operators
# can filter for migration runs in the AWS console.
###############################################################################

locals {
  migration_log_configuration = {
    logDriver = "awslogs"
    options = {
      "awslogs-group"         = var.cloudwatch_log_group_name
      "awslogs-region"        = data.aws_region.current.name
      "awslogs-stream-prefix" = "migration"
    }
  }
}

###############################################################################
# Migration task - container definition (composed)
#
# A single container per task; the command override invokes Alembic
# directly, bypassing the gunicorn launcher entirely. The task starts,
# alembic runs, the container exits, the task stops. ECS reports the
# container exit code via the StoppedReason field on the stopped task,
# which the CD workflow reads to decide whether to proceed with the
# rolling deployment of the long-running service.
#
# The image is the SAME image as the backend; the entry point is
# overridden via `command` rather than `entryPoint` so tini is preserved
# (tini is the ENTRYPOINT in backend/Dockerfile and is responsible for
# proper signal forwarding even on a short-lived alembic invocation).
#
# `command` shape: tini receives ["alembic", "upgrade", "head"] as the
# child argv after its own "--" sentinel, and execs alembic with that
# argv. Alembic then reads alembic.ini from /app and applies any
# pending migrations.
#
# Resource shape: cpu=0 means "share whatever the task-level CPU permits"
# and is correct for single-container task definitions per the comment
# block on backend_container_definitions in locals.tf. The migration is
# I/O-bound on PostgreSQL DDL operations, so 256 CPU + 1024 MiB memory
# (the smallest Fargate combo) is comfortably sufficient even for the
# largest migrations envisioned by the AAP.
#
# stopTimeout=30 mirrors the backend container; alembic should never
# need 30s to gracefully stop, but the consistent value avoids per-task
# tuning surprise.
###############################################################################

locals {
  migration_container_definitions = [
    {
      name      = "migration"
      image     = var.backend_image_uri
      essential = true
      cpu       = 0
      # No portMappings - migrations do not listen on any port.

      # Override the Dockerfile CMD with a direct alembic invocation so
      # the container exits as soon as migrations complete (no gunicorn
      # process is started). The ENTRYPOINT (tini) remains in effect.
      command = ["alembic", "upgrade", "head"]

      environment            = local.migration_environment
      secrets                = local.backend_secrets
      logConfiguration       = local.migration_log_configuration
      readonlyRootFilesystem = false
      stopTimeout            = 30
      # No healthCheck - one-shot tasks do not have a steady state
      # against which to define liveness.
    },
  ]
}

###############################################################################
# Migration task definition
#
# CRITICAL: The family name MUST match what docs/operations.md references
# in its `aws ecs run-task --task-definition` command (lines 81-91 and
# 132-138). The pattern is "<name_prefix>-migration"; for prod that
# yields "sales-connections-prod-migration" exactly as documented.
#
# The migration uses the SAME task and execution roles as the long-running
# backend task because both already have the four-secret IAM grants and
# the log-write grant; the migration needs ALL of those to bootstrap the
# Flask app config object that Alembic env.py imports. Defining a separate
# pair of roles would duplicate the IAM grants and is unjustified at this
# scale (per AAP Sec 0.7.4 the principle is "least-privilege", not
# "one-role-per-task-shape"; the four-secret grant IS the least privilege
# set and is required by both task shapes).
#
# Resource sizing: cpu = 256 (0.25 vCPU), memory = 1024 (1 GiB), the
# smallest Fargate combination. Migrations are bound by RDS DDL latency,
# not CPU; over-provisioning the task adds cost without speeding the
# migration. If a future migration needs more memory (e.g., for a large
# data-migration phase), bump these values via a new migration_task_cpu /
# migration_task_memory variable rather than reusing the long-running
# task's larger sizing.
#
# CRITICAL (AAP Sec 0.7.4): the `secrets` block references Secrets Manager
# ARNs via valueFrom; plaintext secret values NEVER appear in the task
# definition JSON.
###############################################################################

resource "aws_ecs_task_definition" "migration" {
  family                   = "${var.name_prefix}-migration"
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"

  # Smallest valid Fargate CPU/memory combo. Migrations are I/O-bound on
  # RDS DDL ops, not CPU-bound, so 0.25 vCPU + 1 GiB is comfortably
  # sufficient. AAP Sec 0.7.3 budgets do not include migrations (they
  # are out-of-band of user-facing latency).
  cpu    = 256
  memory = 1024

  task_role_arn      = aws_iam_role.task.arn
  execution_role_arn = aws_iam_role.execution.arn

  container_definitions = jsonencode(local.migration_container_definitions)

  tags = merge(local.module_tags, {
    Name = "${var.name_prefix}-migration-task-def"
    Tier = "Migration"
  })

  # Allow the CD pipeline to register new revisions of the migration task
  # definition (with bumped image tags) via `aws ecs register-task-
  # definition` without Terraform reverting them on the next plan/apply.
  # Mirrors the same convention applied to the long-running backend
  # service in main.tf.
  lifecycle {
    ignore_changes = [
      container_definitions,
    ]
  }
}
