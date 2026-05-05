###############################################################################
# infra/terraform/modules/observability/locals.tf
#
# Module-internal locals derived from input variables and same-module
# resources. Centralized here so main.tf, dashboard.tf, and data.tf consume
# canonical expressions rather than duplicating the conditional logic.
#
# Locals exposed:
#
#   Naming locals:
#     - backend_log_group_name      : "/ecs/${var.name_prefix}/backend"
#                                     (canonical CloudWatch log group path
#                                     used by the ECS module's awslogs driver)
#     - alb_log_group_name          : "/aws/applicationloadbalancer/
#                                     ${var.name_prefix}" (only used when
#                                     var.alb_access_logs_to_cloudwatch=true)
#     - alb_logs_bucket_name        : "${var.name_prefix}-alb-logs"
#                                     (S3 bucket; lowercase + hyphens only;
#                                     globally unique by AWS account)
#     - sns_topic_name              : "${var.name_prefix}-alarms"
#     - metric_namespace            : "SalesConnections/${var.environment}"
#                                     (custom CloudWatch namespace for log-
#                                     derived metrics)
#
#   Tag merging:
#     - module_tags                 : merge(var.tags, {
#                                       Component   = "Observability",
#                                       Environment = var.environment,
#                                       ManagedBy   = "Terraform",
#                                       Module      = "infra/terraform/modules/observability"
#                                     })
#
#   KMS coercion:
#     - kms_key_id_or_null          : null when var.kms_key_id == "";
#                                     used by aws_cloudwatch_log_group.X.kms_key_id
#     - kms_master_key_id_sns       : "alias/aws/sns" when var.kms_key_id == "";
#                                     var.kms_key_id otherwise; used by
#                                     aws_sns_topic.alarms.kms_master_key_id
#     - s3_sse_algorithm            : "AES256" when var.kms_key_id == "";
#                                     "aws:kms" otherwise
#     - s3_kms_master_key_id        : null when AES256; var.kms_key_id when KMS
#
#   Alarm action lists:
#     - alarm_default_actions       : [SNS_TOPIC_ARN] (always one element;
#                                     the SNS topic itself fans out to email
#                                     and PagerDuty subscribers)
#     - alarm_ok_actions            : [SNS_TOPIC_ARN] (same as alarm_actions
#                                     so OK transitions also notify)
#
# Most locals are pure functions of input variables (no resource references,
# no data sources). The two alarm-action lists reference
# aws_sns_topic.alarms.arn from main.tf; Terraform's resource graph evaluates
# the SNS topic before the alarms that consume these lists, so the cycle
# is well-defined.
###############################################################################

locals {
  #############################################################################
  # Naming locals
  #
  # CloudWatch log group naming follows AWS conventions:
  #   /ecs/<service-prefix>/<container-name> for ECS task logs
  #   /aws/applicationloadbalancer/<load-balancer-name> for ALB
  # The forward-slash prefix is the canonical AWS pattern; the slash
  # creates a navigable hierarchy in the CloudWatch console.
  #
  # Used by:
  #   - aws_cloudwatch_log_group.backend.name (main.tf)
  #   - dashboard.tf widget references for log-derived metrics
  #   - the ECS module's awslogs-group driver argument (consumed via the
  #     backend_log_group_name output)
  #############################################################################
  backend_log_group_name = "/ecs/${var.name_prefix}/backend"
  alb_log_group_name     = "/aws/applicationloadbalancer/${var.name_prefix}"

  # S3 bucket naming: ALB log delivery requires a lowercase-only,
  # globally-unique name. Combining the org-scoped name_prefix with the
  # "-alb-logs" suffix produces deterministic, recognizable names per
  # environment (e.g., "sales-connections-prod-alb-logs"). The 50-char
  # cap on var.name_prefix in variables.tf leaves room for the 9-char
  # "-alb-logs" suffix within the 63-char S3 bucket limit.
  alb_logs_bucket_name = "${var.name_prefix}-alb-logs"

  # SNS topic name: simple "<name_prefix>-alarms" pattern matches the
  # sibling-module convention.
  sns_topic_name = "${var.name_prefix}-alarms"

  # Custom CloudWatch metric namespace for application-derived metrics.
  # Pattern "<Org>/<env>" supports cost-explorer drill-down per environment
  # without colliding with AWS-managed namespaces (e.g., AWS/ApplicationELB).
  # The slash separator mirrors AWS's own namespace conventions.
  metric_namespace = "SalesConnections/${var.environment}"

  #############################################################################
  # Tag merge: caller-provided var.tags + module-specific labels
  #
  # Per the folder spec authoring convention: every resource merges
  # var.tags plus module-specific labels. We layer in:
  #   - Component   = "Observability" (per folder spec critical constraint)
  #   - Environment = var.environment (cost-explorer drill-down)
  #   - ManagedBy   = "Terraform"     (governance)
  #   - Module      = full module path (auditability)
  #
  # var.tags is the BASE in the merge() call so module-injected labels
  # (Component, Environment, ManagedBy, Module) take precedence over
  # caller-supplied keys with the same name - the module's identity tag
  # must not be overridable.
  #
  # Used by every resource in main.tf and dashboard.tf via
  # merge(local.module_tags, ...).
  #############################################################################
  module_tags = merge(
    var.tags,
    {
      Component   = "Observability"
      Environment = var.environment
      ManagedBy   = "Terraform"
      Module      = "infra/terraform/modules/observability"
    },
  )

  #############################################################################
  # KMS coercion: per-service encryption-key resolution
  #
  # Different AWS services accept KMS key references in different ways:
  #   - aws_cloudwatch_log_group.kms_key_id: accepts null OR a key ARN.
  #     Empty string is rejected by the provider.
  #   - aws_sns_topic.kms_master_key_id: accepts null OR a key alias OR a
  #     key ARN. Empty string is rejected.
  #   - aws_s3_bucket_server_side_encryption_configuration:
  #     - sse_algorithm = "AES256"  -> kms_master_key_id MUST be omitted/null
  #     - sse_algorithm = "aws:kms" -> kms_master_key_id is required
  #
  # We centralize the conditional logic here so each resource argument is
  # a clean local reference.
  #
  # Used by:
  #   - aws_cloudwatch_log_group.backend.kms_key_id (main.tf)
  #   - aws_cloudwatch_log_group.alb_access[0].kms_key_id (main.tf)
  #   - aws_sns_topic.alarms.kms_master_key_id (main.tf)
  #   - aws_s3_bucket_server_side_encryption_configuration.alb_logs.rule (main.tf)
  #############################################################################

  # CloudWatch log group encryption: AWS-managed key (null) by default;
  # CMK ARN when var.kms_key_id is non-empty.
  kms_key_id_or_null = var.kms_key_id != "" ? var.kms_key_id : null

  # SNS topic encryption: AWS-managed alias by default; var.kms_key_id otherwise.
  # Note: aws_sns_topic.kms_master_key_id accepts an alias name (not just ARN);
  # alias/aws/sns is the AWS-managed alias for SNS, always available without
  # provisioning. The folder spec mandates encryption, so we never pass null
  # here (unlike kms_key_id_or_null which leaves AWS to apply alias/aws/logs
  # implicitly).
  kms_master_key_id_sns = var.kms_key_id != "" ? var.kms_key_id : "alias/aws/sns"

  # S3 bucket SSE: AES256 by default; KMS when var.kms_key_id is set.
  # aws:kms with kms_master_key_id_sns referencing var.kms_key_id requires
  # the CMK to grant s3.amazonaws.com via its key policy; provisioning of
  # that policy is the caller's responsibility (typically in the secrets
  # module or a sibling kms module).
  s3_sse_algorithm     = var.kms_key_id != "" ? "aws:kms" : "AES256"
  s3_kms_master_key_id = var.kms_key_id != "" ? var.kms_key_id : null

  #############################################################################
  # Alarm action lists
  #
  # Every CloudWatch alarm gets the same fan-out destination: the SNS topic.
  # The topic then routes to email subscribers and (optionally) the PagerDuty
  # endpoint. Centralizing the list here means adding a new alarm action
  # destination is a one-line change.
  #
  # We use the SAME list for alarm_actions and ok_actions so OK transitions
  # also produce a notification (e.g., "alarm cleared" emails). This is the
  # convention recommended by AWS for alarm hygiene: subscribers know when
  # an issue resolves without checking the dashboard.
  #
  # Reference to aws_sns_topic.alarms.arn:
  #   The aws_sns_topic.alarms resource is declared in main.tf within this
  #   same module. Terraform's resource graph evaluates the topic resource
  #   before any aws_cloudwatch_metric_alarm that consumes these lists,
  #   so the reference is resolved at apply time without an explicit
  #   depends_on.
  #
  # Used by:
  #   - aws_cloudwatch_metric_alarm.<each>.alarm_actions (main.tf)
  #   - aws_cloudwatch_metric_alarm.<each>.ok_actions    (main.tf)
  #############################################################################
  alarm_default_actions = [aws_sns_topic.alarms.arn]
  alarm_ok_actions      = [aws_sns_topic.alarms.arn]
}
