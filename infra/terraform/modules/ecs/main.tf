###############################################################################
# infra/terraform/modules/ecs/main.tf
#
# ECS Fargate compute layer for the Sales-Connections platform:
#
#   1. aws_ecs_cluster.main                          (cluster + Container Insights)
#   2. aws_ecs_cluster_capacity_providers.main       (FARGATE strategy)
#   3. aws_ecs_task_definition.backend               (Flask 3.1.3 + Gunicorn)
#   4. aws_ecs_task_definition.frontend              (CONDITIONAL nginx; count-gated)
#   5. aws_ecs_service.backend                       (registered with ALB target group)
#   6. aws_ecs_service.frontend                      (CONDITIONAL; count-gated)
#   7. aws_security_group.tasks                      (ECS task SG)
#   8. aws_security_group_rule.tasks_ingress_from_alb (ALB -> tasks on app port)
#   9. aws_security_group_rule.tasks_ingress_frontend_from_alb
#                                                    (CONDITIONAL ALB -> tasks
#                                                     on frontend port)
#  10. aws_security_group_rule.tasks_egress_all      (tasks -> world for ECR,
#                                                     Secrets Manager, RDS,
#                                                     Anthropic, Google OAuth)
#
# IAM roles (task role + execution role) and IAM policies live in iam.tf
# per the folder spec readability convention.
#
# Critical constraints (per folder spec / AAP):
#   - Container Insights enabled (Observability rule).
#   - Task definition's `secrets` block references Secrets Manager ARNs;
#     plaintext values NEVER appear in the task definition JSON. The JSON
#     is composed in locals.tf and rendered via jsonencode() here so secrets
#     stay out of this file entirely.
#   - Task definition uses Python-based health check (no curl dependency).
#   - assign_public_ip = false; tasks live in private subnets only.
#   - lifecycle { ignore_changes = [task_definition, desired_count] } on
#     the service so CD revisions and autoscaling don't cause Terraform drift.
#   - enable_execute_command defaults FALSE (production safety).
#
# Build order dependencies (per AAP Sec 0.5.2):
#   Inputs from modules/network:       vpc_id, private_subnet_ids
#   Inputs from modules/ecr:           backend_image_uri, frontend_image_uri
#                                      (consumed transitively by locals.tf)
#   Inputs from modules/secrets:       four secret ARNs
#                                      (consumed transitively by locals.tf)
#   Inputs from modules/database:      db_host, db_port, db_name, db_username
#                                      (consumed transitively by locals.tf)
#   Inputs from modules/alb:           alb_target_group_arn, alb_security_group_id
#   Inputs from modules/observability: cloudwatch_log_group_name
#                                      (consumed transitively by locals.tf)
#   Outputs consumed by:
#     - modules/database (security_group_id for RDS ingress)
#     - modules/secrets  (task_role_arn for secret read perms)
#     - modules/ecr      (execution_role_arn for image pulls)
#     - modules/alb      (security_group_id for ALB egress)
#     - modules/observability (cluster_name, service_name for alarms)
###############################################################################

###############################################################################
# ECS Cluster
#
# Container Insights enabled per the Observability rule (AAP Sec 0.7.5):
# emits CloudWatch metrics for cluster CPU, memory, network, and task counts
# with no application-side instrumentation. The metrics feed the dashboard
# created in modules/observability/.
###############################################################################

resource "aws_ecs_cluster" "main" {
  name = "${var.name_prefix}-cluster"

  setting {
    name  = "containerInsights"
    value = "enabled"
  }

  tags = merge(local.module_tags, {
    Name = "${var.name_prefix}-cluster"
  })
}

###############################################################################
# Capacity Provider Strategy
#
# Default to FARGATE for all environments. FARGATE_SPOT is intentionally
# omitted from MVP scope: the AAP Sec 0.4.9 explicitly excludes auto-scaling
# rules and the Spot interruption model adds complexity for marginal MVP
# cost savings.
###############################################################################

resource "aws_ecs_cluster_capacity_providers" "main" {
  cluster_name       = aws_ecs_cluster.main.name
  capacity_providers = ["FARGATE"]

  default_capacity_provider_strategy {
    capacity_provider = "FARGATE"
    weight            = 100
    base              = 0
  }
}

###############################################################################
# Backend Task Definition (Flask 3.1.3 + Gunicorn)
#
# CRITICAL: container_definitions uses jsonencode() with the structure built
# in locals.tf via local.backend_container_definitions. Plaintext secret
# values NEVER appear here; the `secrets` block (composed inside
# local.backend_container_definitions) references Secrets Manager ARNs that
# ECS resolves at task start, injecting the values as environment variables
# (visible only inside the container, never in CloudTrail or Terraform
# state).
#
# CPU and memory are validated against Fargate's enum in variables.tf via
# the validation block on var.task_cpu. Cross-product compatibility (e.g.,
# 256 CPU only supports 1024-4096 MiB memory) is enforced by AWS at apply
# time since Terraform 1.7 does not support cross-variable validation.
#
# Health check uses Python's stdlib urllib.request (no curl/wget dependency
# in the slim base image); see local.backend_health_check in locals.tf.
# The 30 s startPeriod gives Gunicorn workers and Alembic migrations time
# to bootstrap.
#
# task_role_arn      -> aws_iam_role.task.arn       (runtime application identity)
# execution_role_arn -> aws_iam_role.execution.arn  (bootstrap identity for
#                                                    image pull + secret
#                                                    resolution + log
#                                                    streaming)
# Both roles defined in iam.tf per the AAP Sec 0.4.6 two-role separation.
###############################################################################

resource "aws_ecs_task_definition" "backend" {
  family                   = "${var.name_prefix}-backend"
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  cpu                      = var.task_cpu
  memory                   = var.task_memory

  task_role_arn      = aws_iam_role.task.arn
  execution_role_arn = aws_iam_role.execution.arn

  container_definitions = jsonencode(local.backend_container_definitions)

  tags = merge(local.module_tags, {
    Name = "${var.name_prefix}-backend-task-def"
    Tier = "Backend"
  })
}

###############################################################################
# Frontend Task Definition (CONDITIONAL nginx static delivery)
#
# Created only when var.deploy_frontend_container == true. When false (the
# default), the frontend SPA is delivered as static assets via S3 +
# CloudFront (handled by a future post-MVP module per AAP Sec 0.6.1 stub)
# and this task definition is not provisioned.
#
# Frontend tasks have NO secrets block (the SPA is fully static; all API
# credentials live server-side per AAP Sec 0.7.4 invariant: "Anthropic API
# credential held server-side only"). The frontend container definition
# in locals.tf reflects this: secrets = [].
#
# The same task role and execution role are reused across both task
# definitions: the execution role is necessary for image pulls and log
# streaming on either task; the task role is reused for log-write
# consistency only (the frontend does not exercise its
# secretsmanager:GetSecretValue grant - that grant is still bounded to
# the four supplied ARNs by the inline IAM policy in iam.tf).
###############################################################################

resource "aws_ecs_task_definition" "frontend" {
  count = var.deploy_frontend_container ? 1 : 0

  family                   = "${var.name_prefix}-frontend"
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  cpu                      = var.task_cpu
  memory                   = var.task_memory

  task_role_arn      = aws_iam_role.task.arn
  execution_role_arn = aws_iam_role.execution.arn

  container_definitions = jsonencode(local.frontend_container_definitions)

  tags = merge(local.module_tags, {
    Name = "${var.name_prefix}-frontend-task-def"
    Tier = "Frontend"
  })
}

###############################################################################
# ECS Task Security Group
#
# Defines the network boundary for ECS Fargate tasks:
#   - INGRESS: Permitted only from the ALB security group on the backend
#     container port (default 8000). When the frontend container is also
#     deployed, an additional ingress on the frontend port is created.
#     No CIDR-based ingress; no 0.0.0.0/0.
#   - EGRESS: All-allow because tasks need to reach:
#       - ECR (image pulls during cold start)
#       - Secrets Manager (secret resolution at task start)
#       - RDS (port 5432; the database SG independently restricts ingress
#         to this SG only via modules/database/)
#       - CloudWatch Logs (awslogs driver streaming)
#       - Anthropic Claude API (HTTPS to api.anthropic.com)
#       - Google OAuth (HTTPS to oauth2.googleapis.com)
#       - OTLP exporter endpoint (configurable)
#     Restricting egress to specific CIDRs would require maintaining an
#     allowlist of AWS service IPs and external API endpoints - a brittle
#     and high-maintenance pattern. The defense-in-depth instead relies on
#     RDS-side ingress restriction and the absence of public IPs on tasks
#     (assign_public_ip = false on every aws_ecs_service.network_configuration
#     below).
###############################################################################

resource "aws_security_group" "tasks" {
  name_prefix = "${var.name_prefix}-tasks-"
  description = "ECS task SG; ingress from ALB on ${var.backend_container_port}; egress to RDS, Secrets Manager, ECR, Anthropic, Google"
  vpc_id      = var.vpc_id

  tags = merge(local.module_tags, {
    Name = "${var.name_prefix}-tasks-sg"
  })

  # name_prefix requires create_before_destroy to avoid name collisions on
  # SG replacement (since the suffix is generated by AWS).
  lifecycle {
    create_before_destroy = true
  }
}

###############################################################################
# Ingress rule: ALB -> tasks on the backend container port
#
# Using aws_security_group_rule (separate-from-aws_security_group) rather
# than inline ingress blocks because:
#   1. Avoids cycle-detection issues when the source SG is owned by another
#      module (modules/alb/).
#   2. Makes the rule independently destroyable without recreating the SG.
###############################################################################

resource "aws_security_group_rule" "tasks_ingress_from_alb" {
  type                     = "ingress"
  description              = "HTTP from ALB to backend container port"
  from_port                = var.backend_container_port
  to_port                  = var.backend_container_port
  protocol                 = "tcp"
  security_group_id        = aws_security_group.tasks.id
  source_security_group_id = var.alb_security_group_id
}

###############################################################################
# Frontend ingress rule (CONDITIONAL): ALB -> tasks on the frontend port
#
# Only created when var.deploy_frontend_container == true. Allows the ALB
# to forward HTTP to the nginx container on var.frontend_container_port
# (default 80). When the frontend is delivered via S3+CloudFront instead
# of Fargate, this rule is omitted and the ALB only routes to the backend.
###############################################################################

resource "aws_security_group_rule" "tasks_ingress_frontend_from_alb" {
  count = var.deploy_frontend_container ? 1 : 0

  type                     = "ingress"
  description              = "HTTP from ALB to frontend container port"
  from_port                = var.frontend_container_port
  to_port                  = var.frontend_container_port
  protocol                 = "tcp"
  security_group_id        = aws_security_group.tasks.id
  source_security_group_id = var.alb_security_group_id
}

###############################################################################
# Egress rule: tasks -> world (all)
#
# Permissive egress because tasks need outbound access to multiple AWS
# services (ECR, Secrets Manager, RDS, CloudWatch Logs) AND to external APIs
# (Anthropic, Google OAuth). RDS ingress is restricted at the database
# security group level; AWS service traffic is constrained by IAM policies
# (least-privilege task role); external API access is bound by the
# requirement that tasks have NO public IP (assign_public_ip = false).
###############################################################################

resource "aws_security_group_rule" "tasks_egress_all" {
  type              = "egress"
  description       = "All egress (ECR, Secrets Manager, RDS, CloudWatch, Anthropic, Google, OTLP)"
  from_port         = 0
  to_port           = 0
  protocol          = "-1"
  security_group_id = aws_security_group.tasks.id
  cidr_blocks       = ["0.0.0.0/0"]
}

###############################################################################
# Backend ECS Service
#
# Deploys the backend task definition with:
#   - desired_count tasks running on FARGATE
#   - tasks placed in private subnets (no public IPs)
#   - tasks attached to the ALB target group on the backend container port
#   - rolling deployment with min healthy percent 100, max percent 200
#     (enables zero-downtime deploys at desired_count >= 2)
#   - lifecycle.ignore_changes = [task_definition, desired_count] so:
#     * CD pipeline can bump task definition revisions without Terraform drift
#     * Future autoscaling rules can adjust desired_count without Terraform drift
#
# enable_execute_command (ECS Exec) defaults FALSE per AAP Sec 0.6.1 folder
# spec critical constraint: production should not allow shell access without
# explicit operator opt-in. Dev/staging environments may flip this to true
# via var.enable_execute_command for debugging; the corresponding SSM IAM
# grants on the task role are added in iam.tf when this flag is true.
#
# propagate_tags = "SERVICE" copies the service's tags onto the tasks it
# launches, enabling cost-explorer drill-down by Component / Environment /
# Tier across all running task instances.
###############################################################################

resource "aws_ecs_service" "backend" {
  name            = "${var.name_prefix}-backend"
  cluster         = aws_ecs_cluster.main.id
  task_definition = aws_ecs_task_definition.backend.arn
  desired_count   = var.desired_count
  launch_type     = "FARGATE"

  enable_execute_command = var.enable_execute_command

  network_configuration {
    subnets          = var.private_subnet_ids
    security_groups  = [aws_security_group.tasks.id]
    assign_public_ip = false
  }

  load_balancer {
    target_group_arn = var.alb_target_group_arn
    container_name   = "backend"
    container_port   = var.backend_container_port
  }

  deployment_minimum_healthy_percent = 100
  deployment_maximum_percent         = 200

  # ---------------------------------------------------------------------------
  # Deployment circuit breaker (auto-rollback)
  # ---------------------------------------------------------------------------
  # Per the Checkpoint 4 review finding, ECS deployments must enable the
  # native deployment circuit breaker so that a failed image rollout
  # auto-reverts to the last known-good task definition rather than
  # looping the failed deploy indefinitely. The circuit breaker counts
  # consecutive task failures during a deploy; when the threshold is
  # exceeded ECS marks the deployment failed AND (with rollback = true)
  # reverts to the previous successful task definition without operator
  # intervention.
  #
  # rollback = true is essential: enable alone only marks the deployment
  # failed; without rollback, the bad tasks remain stopped and the
  # service degrades (no replacement tasks are spawned). With rollback
  # the service automatically returns to the prior healthy state.
  #
  # Per AWS ECS documentation, this block is a SOFT alternative to the
  # ECS Blue/Green CodeDeploy controller; it costs nothing extra and
  # works with the rolling-update deployment controller already in use
  # here. Production-grade ECS services (per the AAP Sec 0.7.3
  # reliability budgets) MUST configure it.
  deployment_circuit_breaker {
    enable   = true
    rollback = true
  }

  propagate_tags = "SERVICE"

  tags = merge(local.module_tags, {
    Name = "${var.name_prefix}-backend"
    Tier = "Backend"
  })

  # CRITICAL: Allow CD pipeline to bump task definition revisions and any
  # future autoscaling to adjust desired_count without Terraform drift.
  # Per AAP Sec 0.6.1 folder spec: "lifecycle { ignore_changes =
  # [task_definition, desired_count] }".
  lifecycle {
    ignore_changes = [
      task_definition,
      desired_count,
    ]
  }

  # The ALB target group must exist (and be associated with a listener)
  # before the service can register. The listener association lives in
  # modules/alb/; here we depend on the SG ingress rule that allows
  # ALB -> tasks (without it, tasks would fail health checks during
  # initial deploy because the ALB could not reach them).
  depends_on = [
    aws_security_group_rule.tasks_ingress_from_alb,
  ]
}

###############################################################################
# Frontend ECS Service (CONDITIONAL)
#
# Created only when var.deploy_frontend_container == true. The frontend
# service is logically simpler than the backend (no secrets, no DB, no AI):
# nginx serves static SPA assets to the ALB.
#
# When var.deploy_frontend_container == false (the default), the frontend
# is delivered via S3+CloudFront (a post-MVP module per AAP Sec 0.6.1 stub)
# and this service is not provisioned.
#
# NOTE: When the frontend is deployed via this service, an additional ALB
# target group + listener rule is required in modules/alb/. The folder
# spec documents this coordination but the variable interface here only
# requires the backend target group ARN; frontend-specific ALB wiring
# is the responsibility of modules/alb/ when deploy_frontend_container
# = true. By default the frontend service registers with the SAME target
# group as backend - operators can split into two target groups by
# extending modules/alb/ when path-based routing is insufficient.
###############################################################################

resource "aws_ecs_service" "frontend" {
  count = var.deploy_frontend_container ? 1 : 0

  name            = "${var.name_prefix}-frontend"
  cluster         = aws_ecs_cluster.main.id
  task_definition = aws_ecs_task_definition.frontend[0].arn
  desired_count   = var.desired_count
  launch_type     = "FARGATE"

  enable_execute_command = var.enable_execute_command

  network_configuration {
    subnets          = var.private_subnet_ids
    security_groups  = [aws_security_group.tasks.id]
    assign_public_ip = false
  }

  load_balancer {
    target_group_arn = var.alb_target_group_arn
    container_name   = "frontend"
    container_port   = var.frontend_container_port
  }

  deployment_minimum_healthy_percent = 100
  deployment_maximum_percent         = 200

  # ---------------------------------------------------------------------------
  # Deployment circuit breaker (auto-rollback)
  # ---------------------------------------------------------------------------
  # Mirrors the backend service's circuit-breaker configuration above so
  # frontend rollouts that fail health checks (e.g., a Vite build that
  # produces a broken nginx image) auto-revert to the prior known-good
  # revision. See the backend service's block above for the full
  # rationale.
  deployment_circuit_breaker {
    enable   = true
    rollback = true
  }

  propagate_tags = "SERVICE"

  tags = merge(local.module_tags, {
    Name = "${var.name_prefix}-frontend"
    Tier = "Frontend"
  })

  lifecycle {
    ignore_changes = [
      task_definition,
      desired_count,
    ]
  }

  depends_on = [
    aws_security_group_rule.tasks_ingress_frontend_from_alb,
  ]
}
