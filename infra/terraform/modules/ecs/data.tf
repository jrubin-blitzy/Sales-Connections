###############################################################################
# infra/terraform/modules/ecs/data.tf
#
# Data sources consumed by the ECS module. Centralizing data sources here
# (rather than co-locating with consuming resources) follows the pattern
# established in sibling modules (database, secrets, ecr).
#
# Categories:
#   1. Provider environment lookups (partition, region, account)
#   2. IAM trust policy (assume_role) - shared by task and execution roles
#   3. IAM permission policies for the task role (runtime application identity)
#   4. IAM permission policies for the execution role (bootstrap identity)
#   5. Conditional KMS permission policy (when var.secrets_kms_key_id != "")
#
# All policies are authored as aws_iam_policy_document data sources per the
# folder-spec authoring convention: "All IAM policies use
# aws_iam_policy_document data source (no inline JSON strings in main.tf)".
#
# Cross-file consumption contract:
#   - iam.tf consumes:
#       data.aws_iam_policy_document.ecs_tasks_assume_role.json
#         -> aws_iam_role.task.assume_role_policy
#         -> aws_iam_role.execution.assume_role_policy
#       data.aws_iam_policy_document.task_secrets_read.json
#         -> aws_iam_role_policy.task_secrets_read.policy
#       data.aws_iam_policy_document.task_logs_write.json
#         -> aws_iam_role_policy.task_logs_write.policy
#       data.aws_iam_policy_document.execution_secrets_resolve.json
#         -> aws_iam_role_policy.execution_secrets_resolve.policy
#       data.aws_iam_policy_document.execution_kms[0].json
#         -> aws_iam_role_policy.execution_kms[0].policy (count-gated)
#
#   - locals.tf / main.tf consume:
#       data.aws_region.current.name
#         -> awslogs-region log driver option in container definitions
#       data.aws_partition.current.partition
#         -> portable IAM ARN construction across partitions
#       data.aws_caller_identity.current.account_id
#         -> account-scoped policy conditions and ARN construction
#
# Security invariants (per AAP Sec 0.7.4):
#   - secretsmanager:GetSecretValue is scoped to EXACTLY the four secret
#     ARNs supplied via variables. No wildcards.
#   - logs:* permissions are scoped to the supplied log group and its
#     descendant streams only. No wildcards.
#   - kms:Decrypt (when granted) is scoped to the supplied CMK ARN AND
#     gated by the kms:ViaService condition pinned to Secrets Manager
#     (defense-in-depth: a compromised execution role cannot use the
#     grant against arbitrary KMS-encrypted resources elsewhere).
#   - The assume-role trust policy is pinned to OUR account ID via the
#     aws:SourceAccount condition (confused-deputy mitigation).
###############################################################################

###############################################################################
# Provider environment lookups
#
# These three data sources perform no costly AWS API call - the AWS
# provider derives the values locally from the configured region and the
# active session. Including them costs nothing and makes the module
# portable across:
#   - Standard AWS partition ("aws")
#   - GovCloud partition  ("aws-us-gov")
#   - China partition     ("aws-cn")
# without modification.
#
# Used for:
#   - data.aws_partition.current.partition: portable IAM ARN construction
#     across aws, aws-us-gov, and aws-cn partitions.
#   - data.aws_region.current.name: awslogs-region log driver option in
#     container definitions (locals.tf) and the kms:ViaService condition
#     in execution_kms below.
#   - data.aws_caller_identity.current.account_id: aws:SourceAccount
#     confused-deputy mitigation in the assume-role trust policy and
#     log-group ARN construction in task_logs_write below.
###############################################################################

data "aws_partition" "current" {}

data "aws_region" "current" {}

data "aws_caller_identity" "current" {}

###############################################################################
# Assume-Role Trust Policy (shared by task and execution roles)
#
# Both roles allow ECS tasks (the "ecs-tasks.amazonaws.com" service
# principal) to assume them. ECS uses this trust during task launch:
#   - Execution role: ECS itself assumes to pull the image from ECR,
#     resolve the `secrets` block at task start, and stream container
#     stdout/stderr via the awslogs Docker driver.
#   - Task role: The container runtime assumes for the application's
#     own AWS API calls (e.g., boto3.client('secretsmanager') inside the
#     Flask app for runtime secret reads).
#
# The aws:SourceAccount condition prevents the "confused deputy" attack
# pattern where another AWS account could induce ECS to assume our role
# for tasks running in their account. The condition pins the trust to
# OUR account only.
#
# Reference: AWS docs - "How to use trust policies with IAM roles"
#            (Cross-service confused deputy problem section)
###############################################################################

data "aws_iam_policy_document" "ecs_tasks_assume_role" {
  statement {
    sid     = "AllowEcsTasksToAssumeRole"
    effect  = "Allow"
    actions = ["sts:AssumeRole"]

    principals {
      type        = "Service"
      identifiers = ["ecs-tasks.amazonaws.com"]
    }

    condition {
      test     = "StringEquals"
      variable = "aws:SourceAccount"
      values   = [data.aws_caller_identity.current.account_id]
    }
  }
}

###############################################################################
# Task Role Permission Policy: Secrets Manager read
#
# Grants secretsmanager:GetSecretValue on EXACTLY the four secret ARNs
# supplied via variables. This is the AAP Sec 0.7.4 least-privilege
# invariant: "secretsmanager:GetSecretValue on the task role is scoped to
# ONLY the four secret ARNs supplied via variables; never *".
#
# DescribeSecret is included because the boto3 SDK's get_secret_value
# default error path issues a DescribeSecret to surface the failure mode
# (e.g., distinguishing "secret does not exist" from "access denied"). It
# carries no additional risk because the resource scope is identical -
# both actions are pinned to the same four secret ARNs.
#
# Why is the task role's Secrets Manager grant separate from the
# execution role's? The execution role resolves secrets at TASK START
# (before the container runs) for the ECS native `secrets` block. The
# task role resolves secrets at WORKER STARTUP from inside the running
# container. Both code paths exist; both need the grant.
###############################################################################

data "aws_iam_policy_document" "task_secrets_read" {
  statement {
    sid    = "ReadFourApplicationSecrets"
    effect = "Allow"

    actions = [
      "secretsmanager:GetSecretValue",
      "secretsmanager:DescribeSecret",
    ]

    resources = [
      var.secret_anthropic_api_key_arn,
      var.secret_google_oauth_client_secret_arn,
      var.secret_jwt_signing_key_arn,
      var.secret_db_password_arn,
    ]
  }
}

###############################################################################
# Task Role Permission Policy: CloudWatch Logs write
#
# Allows the application code (running under the task role) to create log
# streams within the supplied log group and put log events. The structlog
# JSON formatter writes via the awslogs Docker driver (which uses the
# execution role's permissions, granted via the AWS-managed
# AmazonECSTaskExecutionRolePolicy attached in iam.tf), but the
# application may also explicitly publish via boto3 for non-stdout streams
# (e.g., a future audit-trail mirror or explicit error-tier emission).
# Both code paths exist; both need the grant under their respective
# IAM principal.
#
# Resource scope is the supplied log group AND its descendant streams
# (the ":*" suffix). This is the canonical CloudWatch Logs scoping
# pattern:
#   - logs:CreateLogStream and logs:DescribeLogStreams operate on the
#     log group ARN.
#   - logs:PutLogEvents operates on the log stream ARN, which has the
#     form "<log-group-arn>:log-stream:<stream-name>" - the ":*" wildcard
#     covers all streams within the group.
#
# Partition-aware ARN construction uses three data sources to build a
# portable ARN that works across AWS standard, GovCloud, and China
# partitions without modification:
#   arn:${partition}:logs:${region}:${account}:log-group:<name>[:*]
###############################################################################

data "aws_iam_policy_document" "task_logs_write" {
  statement {
    sid    = "WriteToBackendLogGroup"
    effect = "Allow"

    actions = [
      "logs:CreateLogStream",
      "logs:PutLogEvents",
      "logs:DescribeLogStreams",
    ]

    resources = [
      "arn:${data.aws_partition.current.partition}:logs:${data.aws_region.current.name}:${data.aws_caller_identity.current.account_id}:log-group:${var.cloudwatch_log_group_name}",
      "arn:${data.aws_partition.current.partition}:logs:${data.aws_region.current.name}:${data.aws_caller_identity.current.account_id}:log-group:${var.cloudwatch_log_group_name}:*",
    ]
  }
}

###############################################################################
# Execution Role Permission Policy: Secrets resolution at task start
#
# When the task definition's `secrets` block references Secrets Manager
# ARNs, ECS itself reads the values at task start and injects them as
# environment variables into the container before the application starts.
# This requires secretsmanager:GetSecretValue on the supplied ARNs
# granted to the execution role (which ECS assumes during the task
# launch sequence).
#
# Note this is the same action+resource set as the task role's
# secrets-read policy (above), but the use cases are distinct:
#   - Execution role: ECS resolves at TASK START (before container runs).
#                     This is the standard injection path for the
#                     ANTHROPIC_API_KEY, GOOGLE_OAUTH_CLIENT_SECRET,
#                     JWT_SIGNING_KEY, and DB_PASSWORD env vars.
#   - Task role:      Application code resolves at WORKER STARTUP (after
#                     container starts). Used for any boto3-based reads
#                     the app performs at process startup.
#
# DescribeSecret is intentionally NOT included on the execution role -
# ECS's secret-resolution code path goes directly to GetSecretValue and
# does not issue a separate DescribeSecret. Granting only what's needed
# is the least-privilege ideal.
###############################################################################

data "aws_iam_policy_document" "execution_secrets_resolve" {
  statement {
    sid    = "ResolveFourApplicationSecretsAtTaskStart"
    effect = "Allow"

    actions = [
      "secretsmanager:GetSecretValue",
    ]

    resources = [
      var.secret_anthropic_api_key_arn,
      var.secret_google_oauth_client_secret_arn,
      var.secret_jwt_signing_key_arn,
      var.secret_db_password_arn,
    ]
  }
}

###############################################################################
# Execution Role Permission Policy: KMS Decrypt (CONDITIONAL)
#
# When secrets are encrypted with a customer-managed CMK (vs the AWS-
# managed `aws/secretsmanager` key), the execution role needs explicit
# kms:Decrypt permission on that CMK to resolve the secret values at
# task start.
#
# When var.secrets_kms_key_id is "" (default), secrets use the AWS-
# managed key and no additional kms:Decrypt grant is needed - the
# AWS-managed key allows decryption to any principal in the account that
# already has secretsmanager:GetSecretValue on the corresponding secret
# ARN. In that case this data source produces zero documents (count = 0)
# and the consuming aws_iam_role_policy.execution_kms in iam.tf is also
# count-gated to zero.
#
# kms:DescribeKey is included alongside kms:Decrypt because the AWS SDK
# (and ECS's secret-resolution code path) issues a DescribeKey call to
# inspect the key's metadata (e.g., enabled-state, key-spec). Without it,
# the resolution path may degrade to opaque error messages.
#
# Defense-in-depth: the kms:ViaService condition restricts the grant to
# decryption requests initiated through the Secrets Manager service
# (i.e., requests where Secrets Manager is the calling service on
# behalf of the principal). This means a compromised execution role
# cannot use this kms:Decrypt grant to decrypt arbitrary KMS-encrypted
# resources elsewhere in the account that happen to use the same CMK -
# only the secrets-resolution pathway is admitted.
#
# Count-gating strategy:
#   - var.secrets_kms_key_id == "" -> count = 0, document is an empty
#     list, consuming aws_iam_role_policy.execution_kms[0] in iam.tf
#     is also count-gated and produces no resource.
#   - var.secrets_kms_key_id != "" -> count = 1, document is a 1-element
#     list, consuming aws_iam_role_policy.execution_kms[0].policy reads
#     element 0.
#
# Reference: AWS docs - "Permissions to use a customer managed key with
#            Secrets Manager" (kms:ViaService condition section).
###############################################################################

data "aws_iam_policy_document" "execution_kms" {
  count = var.secrets_kms_key_id != "" ? 1 : 0

  statement {
    sid    = "DecryptCustomerManagedKmsKeyForSecrets"
    effect = "Allow"

    actions = [
      "kms:Decrypt",
      "kms:DescribeKey",
    ]

    resources = [
      var.secrets_kms_key_id,
    ]

    condition {
      test     = "StringEquals"
      variable = "kms:ViaService"
      values   = ["secretsmanager.${data.aws_region.current.name}.amazonaws.com"]
    }
  }
}
