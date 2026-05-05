###############################################################################
# infra/terraform/modules/ecs/iam.tf
#
# IAM roles for ECS Fargate tasks - the two-role separation mandated by
# AAP Sec 0.4.6:
#
#   1. Task Role (aws_iam_role.task):
#      Runtime application identity. Assumed by the application code inside
#      the container via instance metadata. Permissions:
#        - secretsmanager:GetSecretValue on the four secret ARNs (LEAST PRIV)
#        - logs:CreateLogStream + logs:PutLogEvents on the supplied log group
#        - Optional X-Ray tracing exports (managed policy attachment)
#
#   2. Execution Role (aws_iam_role.execution):
#      Bootstrap identity used by ECS itself to launch the task. Permissions:
#        - AmazonECSTaskExecutionRolePolicy (managed) for image pull + logs
#        - secretsmanager:GetSecretValue on the four secret ARNs (so ECS
#          can resolve the `secrets` block at task start)
#        - Optional kms:Decrypt when secrets use a customer-managed CMK
#
# CRITICAL SEPARATION (per AAP Sec 0.4.6):
#   - Task role NEVER has image pull permissions (ECR access).
#   - Execution role NEVER has direct application-data permissions
#     (no DynamoDB write, no S3 read, etc.).
#
# CRITICAL LEAST-PRIVILEGE (per AAP Sec 0.7.4):
#   - secretsmanager:GetSecretValue on BOTH roles is scoped to ONLY the four
#     supplied secret ARNs (variables: secret_anthropic_api_key_arn,
#     secret_google_oauth_client_secret_arn, secret_jwt_signing_key_arn,
#     secret_db_password_arn). Never "Resource = *".
#
# Resource graph (within this file):
#
#   data.aws_iam_policy_document.ecs_tasks_assume_role  (defined in data.tf)
#     |-> aws_iam_role.task.assume_role_policy
#     |-> aws_iam_role.execution.assume_role_policy
#
#   data.aws_iam_policy_document.task_secrets_read       (defined in data.tf)
#     |-> aws_iam_role_policy.task_secrets_read.policy
#
#   data.aws_iam_policy_document.task_logs_write         (defined in data.tf)
#     |-> aws_iam_role_policy.task_logs_write.policy
#
#   data.aws_iam_policy_document.execution_secrets_resolve  (defined in data.tf)
#     |-> aws_iam_role_policy.execution_secrets_resolve.policy
#
#   data.aws_iam_policy_document.execution_kms          (defined in data.tf;
#                                                        count-gated by
#                                                        var.secrets_kms_key_id)
#     |-> aws_iam_role_policy.execution_kms.policy
#
# Resource count summary:
#   - 7 unconditional resources (task role + 3 task policies/attachments,
#     execution role + 2 execution policies/attachments)
#   - 1 conditional resource (execution_kms, count-gated by
#     var.secrets_kms_key_id != "")
#
# Cross-file consumption (referenced by main.tf and outputs.tf):
#   - aws_iam_role.task.arn      -> aws_ecs_task_definition.backend.task_role_arn
#   - aws_iam_role.execution.arn -> aws_ecs_task_definition.backend.execution_role_arn
#   - Both role ARNs surfaced as module outputs for cross-module audit /
#     CloudTrail forensics.
###############################################################################

###############################################################################
# Task Role - Runtime Application Identity
#
# Assumed by the application code inside the container (via the AWS SDK's
# default credential chain that finds ECS task metadata). This is the
# identity used by:
#   - backend/app/config.py (boto3.client('secretsmanager').get_secret_value)
#   - structlog awslogs handler (logs.PutLogEvents)
#   - opentelemetry-exporter-otlp (X-Ray export when configured)
#
# The trust policy is sourced from data.aws_iam_policy_document.ecs_tasks_
# assume_role.json (defined in data.tf), which pins assumption to the
# ecs-tasks.amazonaws.com service principal AND adds an aws:SourceAccount
# condition for confused-deputy mitigation.
###############################################################################

resource "aws_iam_role" "task" {
  name        = "${var.name_prefix}-task-role"
  description = "ECS task runtime role for Sales-Connections backend (least-privilege secret reads + log writes per AAP Sec 0.7.4)"

  assume_role_policy = data.aws_iam_policy_document.ecs_tasks_assume_role.json

  tags = merge(local.module_tags, {
    Name = "${var.name_prefix}-task-role"
    Tier = "Runtime"
  })
}

###############################################################################
# Task Role Policy: Secrets Read
#
# Grants secretsmanager:GetSecretValue ONLY on the four supplied secret ARNs
# (per AAP Sec 0.7.4 least-privilege invariant: "secretsmanager:GetSecretValue
# on the task role is scoped to ONLY the four secret ARNs supplied via
# variables; never *").
#
# The application reads these at startup via boto3.client('secretsmanager')
# and caches them for the worker lifetime (per AAP Sec 0.4.4).
#
# Inline policy (aws_iam_role_policy) - NOT managed-policy attachment -
# because the policy is least-privilege and role-specific. Inline policies
# are scoped to the role lifecycle: when the role is destroyed, the inline
# policy is destroyed with it (no dangling policy artifacts).
###############################################################################

resource "aws_iam_role_policy" "task_secrets_read" {
  name   = "${var.name_prefix}-task-secrets-read"
  role   = aws_iam_role.task.id
  policy = data.aws_iam_policy_document.task_secrets_read.json
}

###############################################################################
# Task Role Policy: CloudWatch Logs Write
#
# Allows the application to create log streams within the supplied log group
# and write log events. The structlog JSON formatter writes via the awslogs
# Docker driver, but the application may also explicitly publish via boto3
# for non-stdout streams (e.g., audit-trail mirror). This permission grants
# both pathways (the awslogs driver uses the EXECUTION role's permissions
# from AmazonECSTaskExecutionRolePolicy; explicit boto3 publishes use the
# TASK role's permissions from this policy).
###############################################################################

resource "aws_iam_role_policy" "task_logs_write" {
  name   = "${var.name_prefix}-task-logs-write"
  role   = aws_iam_role.task.id
  policy = data.aws_iam_policy_document.task_logs_write.json
}

###############################################################################
# Task Role Policy Attachment: X-Ray Daemon Write (optional)
#
# Per AAP Sec 0.6.1 folder spec critical requirements: "Optional:
# aws_iam_role_policy_attachment.task_xray - AWSXRayDaemonWriteAccess for
# OTEL/X-Ray tracing exports". When the OTLP exporter is configured to
# route to AWS X-Ray (vs a self-hosted Jaeger/Tempo), this managed policy
# grants the necessary PutTelemetryRecords permissions.
#
# The OTLP collector runs as a sidecar (or as a separate ADOT collector
# task); the application emits OTLP-format spans which the collector
# translates to X-Ray. Either way, the task role needs this policy when
# X-Ray is the destination.
#
# Granted unconditionally (not count-gated) because the OTLP destination is
# configured at runtime via var.otlp_exporter_endpoint; granting the policy
# unconditionally allows operators to flip the env var without re-applying
# Terraform. The cost is granting an unused permission when X-Ray isn't the
# OTLP destination - acceptable per the folder spec note that this
# attachment is "optional".
#
# Partition-aware ARN construction uses data.aws_partition.current.partition
# so the module is portable across AWS standard ("aws"), GovCloud
# ("aws-us-gov"), and China ("aws-cn") partitions without modification.
###############################################################################

resource "aws_iam_role_policy_attachment" "task_xray" {
  role       = aws_iam_role.task.name
  policy_arn = "arn:${data.aws_partition.current.partition}:iam::aws:policy/AWSXRayDaemonWriteAccess"
}

###############################################################################
# Execution Role - Bootstrap Identity for ECS Task Launch
#
# Used by ECS itself (not by the application code) to:
#   1. Pull the container image from ECR
#   2. Resolve the Secrets Manager `secrets` block at task start, reading
#      each secret ARN's current value and injecting as a container env var
#   3. Stream container stdout/stderr to CloudWatch Logs via awslogs driver
#
# CRITICAL: This role is FUNDAMENTALLY DIFFERENT from the task role. It runs
# during task launch (before the application starts) and has NO permissions
# the application code can use at runtime. The two-role separation is
# defense-in-depth: a compromise of the application code cannot escalate to
# image pull or task launch capabilities.
#
# Trust policy is shared with the task role via the same data source. The
# distinction between the two roles is enforced at the PERMISSION boundary
# (via the inline and managed policies attached below), not at the trust
# boundary - both are assumable by ecs-tasks.amazonaws.com because that is
# how ECS launches Fargate tasks.
###############################################################################

resource "aws_iam_role" "execution" {
  name        = "${var.name_prefix}-execution-role"
  description = "ECS task execution role for Sales-Connections (image pull, log emission, secret resolution per AAP Sec 0.4.6 two-role separation)"

  assume_role_policy = data.aws_iam_policy_document.ecs_tasks_assume_role.json

  tags = merge(local.module_tags, {
    Name = "${var.name_prefix}-execution-role"
    Tier = "Bootstrap"
  })
}

###############################################################################
# Execution Role Policy Attachment: AWS Managed ECS Task Execution Policy
#
# AmazonECSTaskExecutionRolePolicy grants:
#   - ecr:GetAuthorizationToken
#   - ecr:BatchCheckLayerAvailability
#   - ecr:GetDownloadUrlForLayer
#   - ecr:BatchGetImage
#   - logs:CreateLogStream
#   - logs:PutLogEvents
#
# These are the canonical permissions ECS requires to launch a Fargate task.
# Using the managed policy keeps these in lockstep with AWS-side updates
# (e.g., when AWS introduces new ECR API actions for image pulls).
#
# Partition-aware ARN construction lets this module deploy unchanged into
# AWS standard, GovCloud, and China partitions.
###############################################################################

resource "aws_iam_role_policy_attachment" "execution_managed" {
  role       = aws_iam_role.execution.name
  policy_arn = "arn:${data.aws_partition.current.partition}:iam::aws:policy/service-role/AmazonECSTaskExecutionRolePolicy"
}

###############################################################################
# Execution Role Policy: Secrets Resolution at Task Start
#
# When the task definition's `secrets` block references Secrets Manager ARNs,
# ECS itself reads the values at task start and injects them as env vars.
# This requires secretsmanager:GetSecretValue on the supplied ARNs.
#
# Note: This is duplicate of the task role's secrets-read policy in terms of
# the action and resource set, but the use cases are distinct:
#   - Execution role: ECS resolves at TASK START (before container runs).
#   - Task role:      Application code reads at WORKER STARTUP (after
#                     container starts).
#
# Both must be granted because the secrets are accessed via two different
# pathways (env var injection vs. boto3 SDK call), each requiring its own
# IAM principal. The shared resource scope (the four supplied secret ARNs)
# is enforced by both data sources sharing the same variable list in
# data.tf.
###############################################################################

resource "aws_iam_role_policy" "execution_secrets_resolve" {
  name   = "${var.name_prefix}-execution-secrets-resolve"
  role   = aws_iam_role.execution.id
  policy = data.aws_iam_policy_document.execution_secrets_resolve.json
}

###############################################################################
# Execution Role Policy: KMS Decrypt (CONDITIONAL)
#
# Per folder spec: "If var.secrets_kms_key_id != \"\", add kms:Decrypt on
# that key". When secrets are encrypted with a customer-managed CMK (vs.
# the AWS-managed `aws/secretsmanager` key), the execution role needs
# explicit kms:Decrypt permission to resolve the secret values at task
# start.
#
# When var.secrets_kms_key_id is empty, secrets use the AWS-managed key
# which is decryptable by any principal in the account that has
# secretsmanager:GetSecretValue. No additional kms:Decrypt grant is needed.
#
# Count-gating expression mirrors the count on
# data.aws_iam_policy_document.execution_kms in data.tf so the resource
# and its source policy document are created together (or not at all).
# When count = 0, the [0] subscript on the data source reference is
# unreachable at apply time because Terraform short-circuits the
# expression evaluation when count = 0.
###############################################################################

resource "aws_iam_role_policy" "execution_kms" {
  count = var.secrets_kms_key_id != "" ? 1 : 0

  name   = "${var.name_prefix}-execution-kms"
  role   = aws_iam_role.execution.id
  policy = data.aws_iam_policy_document.execution_kms[0].json
}
