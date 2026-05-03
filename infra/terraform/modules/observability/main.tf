###############################################################################
# infra/terraform/modules/observability/main.tf
#
# Core observability resources for the Sales-Connections platform.
#
# Resources provisioned (the dashboard is in dashboard.tf):
#
#   CloudWatch Log Groups:
#     aws_cloudwatch_log_group.backend                     (always)
#     aws_cloudwatch_log_group.alb_access_logs             (count = var.alb_access_logs_to_cloudwatch ? 1 : 0)
#
#   S3 Bucket for ALB Access Logs (always):
#     aws_s3_bucket.alb_access_logs
#     aws_s3_bucket_public_access_block.alb_access_logs
#     aws_s3_bucket_versioning.alb_access_logs
#     aws_s3_bucket_server_side_encryption_configuration.alb_access_logs
#     aws_s3_bucket_lifecycle_configuration.alb_access_logs
#     aws_s3_bucket_policy.alb_access_logs
#
#   SNS Topic + Subscriptions:
#     aws_sns_topic.alarms                                 (always)
#     aws_sns_topic_subscription.email                     (for_each over var.alarm_email_addresses)
#     aws_sns_topic_subscription.pagerduty                 (count = var.enable_pagerduty && var.pagerduty_endpoint != "" ? 1 : 0)
#
#   Log Metric Filters (custom-metric derivation from JSON logs):
#     aws_cloudwatch_log_metric_filter.ai_latency
#     aws_cloudwatch_log_metric_filter.form_submit_latency
#     aws_cloudwatch_log_metric_filter.audit_emit_latency
#
#   CloudWatch Metric Alarms (eight; per AAP Sec 0.7.3):
#     aws_cloudwatch_metric_alarm.ai_latency_p95
#     aws_cloudwatch_metric_alarm.form_submit_p95
#     aws_cloudwatch_metric_alarm.audit_emit_p95
#     aws_cloudwatch_metric_alarm.alb_5xx_rate
#     aws_cloudwatch_metric_alarm.alb_target_unhealthy
#     aws_cloudwatch_metric_alarm.ecs_running_task_count
#     aws_cloudwatch_metric_alarm.rds_cpu
#     aws_cloudwatch_metric_alarm.rds_storage_free
#
# Resource graph (within this module):
#
#   data.aws_iam_policy_document.alb_logs_bucket_policy (defined in data.tf)
#     |-> aws_s3_bucket_policy.alb_access_logs
#
#   aws_sns_topic.alarms
#     |-> aws_sns_topic_subscription.email      (for_each)
#     |-> aws_sns_topic_subscription.pagerduty  (count-gated)
#     |-> alarm_actions on every aws_cloudwatch_metric_alarm
#     |-> ok_actions on every aws_cloudwatch_metric_alarm
#
#   aws_cloudwatch_log_group.backend
#     |-> aws_cloudwatch_log_metric_filter.ai_latency       (log_group_name)
#     |-> aws_cloudwatch_log_metric_filter.form_submit_latency
#     |-> aws_cloudwatch_log_metric_filter.audit_emit_latency
#
# Critical security/architectural invariants:
#   - All resources tagged via merge(local.module_tags, ...).
#   - SNS topic encrypted via local.kms_master_key_id_sns (alias/aws/sns
#     by default; CMK when var.kms_key_id is set).
#   - CloudWatch log groups encrypted via local.kms_key_id_or_null
#     (AWS-managed by default; CMK when var.kms_key_id is set).
#   - S3 bucket encrypted with AES256 (or KMS when var.kms_key_id is set).
#   - S3 bucket public access fully blocked.
#   - S3 bucket policy uses aws_iam_policy_document, not inline JSON.
#   - SNS used ONLY for alarm fan-out (per AAP Sec 0.4.9 synchronous-only).
#
# Total resource declarations in this file: 22
#   2 log groups + 6 S3-related + 3 SNS-related + 3 metric filters + 8 alarms.
###############################################################################

###############################################################################
# Backend ECS task log group
#
# Consumed by modules/ecs/ awslogs driver. The log_group_name follows the
# canonical /ecs/<name_prefix>/backend pattern, mirrored in
# local.backend_log_group_name. Retention is enforced via
# var.log_retention_days (validated to one of CloudWatch's enumerated
# values in variables.tf; never null per the folder spec critical
# constraint).
#
# Encryption: local.kms_key_id_or_null evaluates to null (use AWS-managed
# alias/aws/logs implicitly) when var.kms_key_id == "", or to the supplied
# CMK ARN otherwise. The aws_cloudwatch_log_group resource accepts null
# (encrypted with AWS-managed key) but rejects an empty string.
###############################################################################

resource "aws_cloudwatch_log_group" "backend" {
  name              = local.backend_log_group_name
  retention_in_days = var.log_retention_days
  kms_key_id        = local.kms_key_id_or_null

  tags = merge(local.module_tags, {
    Name = local.backend_log_group_name
    Tier = "Backend"
  })
}

###############################################################################
# Optional ALB access log CloudWatch sink (S3 -> CloudWatch via subscription)
#
# When var.alb_access_logs_to_cloudwatch == true, this log group exists to
# receive a stream of ALB access logs piped from the S3 bucket via a separate
# Lambda subscription (post-MVP; the Lambda is not provisioned in this
# module, but the destination log group is).
#
# Default (false): logs live only in S3; queryable via Athena. The S3
# bucket is always created (see below), so disabling this CloudWatch sink
# does not break ALB access logging - it only forgoes the optional
# CloudWatch Insights query path.
###############################################################################

resource "aws_cloudwatch_log_group" "alb_access_logs" {
  count = var.alb_access_logs_to_cloudwatch ? 1 : 0

  name              = local.alb_log_group_name
  retention_in_days = var.log_retention_days
  kms_key_id        = local.kms_key_id_or_null

  tags = merge(local.module_tags, {
    Name = local.alb_log_group_name
    Tier = "ALB"
  })
}

###############################################################################
# S3 bucket receiving ALB access logs (always created)
#
# The bucket name is local.alb_logs_bucket_name = "${var.name_prefix}-alb-logs"
# (S3 names are globally unique and lowercase-only). The bucket is created
# unconditionally because ALB access logging is enabled by the alb module
# regardless of whether a CloudWatch sink is configured; without this
# bucket, ALB log delivery would fail.
#
# force_destroy is sourced from var.alb_log_bucket_force_destroy: false in
# production (so terraform destroy fails loudly when the bucket has
# objects), true in dev for clean tear-down.
###############################################################################

resource "aws_s3_bucket" "alb_access_logs" {
  bucket        = local.alb_logs_bucket_name
  force_destroy = var.alb_log_bucket_force_destroy

  tags = merge(local.module_tags, {
    Name = local.alb_logs_bucket_name
    Tier = "ALB"
  })
}

###############################################################################
# Public access block: lock the bucket down (defense in depth)
#
# Even though the resource policy already restricts access to the ALB log
# delivery service, block_public_acls + block_public_policy +
# ignore_public_acls + restrict_public_buckets all set to true ensure no
# accidental ACL or policy mutation can expose the bucket publicly. AWS
# evaluates these flags before the bucket policy on every S3 request.
###############################################################################

resource "aws_s3_bucket_public_access_block" "alb_access_logs" {
  bucket = aws_s3_bucket.alb_access_logs.id

  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

###############################################################################
# Versioning: enabled so accidental object deletion is recoverable.
#
# The lifecycle policy below transitions noncurrent (overwritten or deleted)
# versions to expiry after 90 days, capping storage cost while still
# providing a recovery window for accidental deletions during incident
# investigations.
###############################################################################

resource "aws_s3_bucket_versioning" "alb_access_logs" {
  bucket = aws_s3_bucket.alb_access_logs.id

  versioning_configuration {
    status = "Enabled"
  }
}

###############################################################################
# Server-side encryption
#
# Default: AES256 (SSE-S3, AWS-managed). When var.kms_key_id is set, switches
# to aws:kms with the supplied CMK. local.s3_sse_algorithm and
# local.s3_kms_master_key_id encapsulate the conditional logic.
#
# bucket_key_enabled = true reduces KMS API calls (and costs) by 99%+ for
# SSE-KMS workloads. No effect when SSE is AES256, but no harm either; the
# flag is set unconditionally so switching to KMS is a single-variable
# change with no resource churn.
###############################################################################

resource "aws_s3_bucket_server_side_encryption_configuration" "alb_access_logs" {
  bucket = aws_s3_bucket.alb_access_logs.id

  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm     = local.s3_sse_algorithm
      kms_master_key_id = local.s3_kms_master_key_id
    }
    bucket_key_enabled = true
  }
}

###############################################################################
# Lifecycle policy: tier old logs to Infrequent Access, then Glacier, then
# expire to control cost. ALB access logs are typically queried hot for
# a few days, then archived for compliance / forensics.
#
# Tiering schedule:
#   Day 0-30:   STANDARD     (hot - free queries via Athena)
#   Day 30-90:  STANDARD_IA  (warm - per-retrieval fee but cheaper storage)
#   Day 90-365: GLACIER      (cold - hours-to-restore retrieval)
#   Day 365+:   expired      (deleted)
#
# Noncurrent (overwritten/deleted) versions are expired after 90 days.
# Incomplete multipart uploads are aborted after 7 days so failed uploads
# do not accumulate cost indefinitely.
###############################################################################

resource "aws_s3_bucket_lifecycle_configuration" "alb_access_logs" {
  bucket = aws_s3_bucket.alb_access_logs.id

  rule {
    id     = "alb-logs-tiering"
    status = "Enabled"

    # Empty filter applies the rule to all objects in the bucket.
    filter {}

    transition {
      days          = 30
      storage_class = "STANDARD_IA"
    }

    transition {
      days          = 90
      storage_class = "GLACIER"
    }

    expiration {
      days = 365
    }

    noncurrent_version_expiration {
      noncurrent_days = 90
    }

    abort_incomplete_multipart_upload {
      days_after_initiation = 7
    }
  }
}


###############################################################################
# Bucket policy: allow the ALB log delivery service to put objects.
#
# Per AWS docs, ALB log delivery uses one of two principals depending on
# the region:
#   - Older regions: a region-specific AWS account ID (looked up via
#     data.aws_elb_service_account.current)
#   - Newer regions: the logdelivery.elasticloadbalancing.amazonaws.com
#     service principal
#
# Both principals are encoded in data.aws_iam_policy_document.alb_logs_bucket_policy
# (defined in data.tf), along with a third statement granting GetBucketAcl
# to the service principal so the access-log enablement bootstrap succeeds.
#
# depends_on enforces ordering: the public access block must be applied
# first; otherwise AWS may reject the policy as potentially-public and the
# apply fails with an unhelpful error.
###############################################################################

resource "aws_s3_bucket_policy" "alb_access_logs" {
  bucket = aws_s3_bucket.alb_access_logs.id
  policy = data.aws_iam_policy_document.alb_logs_bucket_policy.json

  depends_on = [aws_s3_bucket_public_access_block.alb_access_logs]
}

###############################################################################
# SNS topic for alarm fan-out
#
# Per AAP Sec 0.4.9, this topic is for ALARM FAN-OUT ONLY. It must NEVER be
# used for application message passing - the project's synchronous-only
# integration stance forbids message queues, event buses, and pub/sub
# patterns for application traffic.
#
# Subscriptions:
#   - One email subscription per entry in var.alarm_email_addresses (for_each)
#   - One PagerDuty subscription when var.enable_pagerduty == true and
#     var.pagerduty_endpoint is non-empty (count-gated)
#
# Encryption: local.kms_master_key_id_sns evaluates to "alias/aws/sns" by
# default (AWS-managed key, always available without provisioning), or to
# var.kms_key_id when a CMK is supplied. The folder spec mandates
# encryption, so we never pass null here.
###############################################################################

resource "aws_sns_topic" "alarms" {
  name              = local.sns_topic_name
  kms_master_key_id = local.kms_master_key_id_sns

  tags = merge(local.module_tags, {
    Name    = local.sns_topic_name
    Purpose = "AlarmFanOut"
  })
}

###############################################################################
# Email subscriptions (one per address in var.alarm_email_addresses)
#
# AWS sends a confirmation email to each address; subscriptions remain
# PendingConfirmation until the recipient clicks the confirmation link.
# Terraform tracks subscriptions as Active in state once AWS reports the
# subscription as confirmed.
#
# Using for_each = toset(var.alarm_email_addresses) means each subscription
# is keyed by the email address itself, so adding/removing emails does not
# churn unrelated subscriptions (which would happen with count-based
# indexing if the list were re-ordered).
###############################################################################

resource "aws_sns_topic_subscription" "email" {
  for_each = toset(var.alarm_email_addresses)

  topic_arn = aws_sns_topic.alarms.arn
  protocol  = "email"
  endpoint  = each.value
}

###############################################################################
# PagerDuty subscription (conditional)
#
# Per the folder spec critical constraint, var.pagerduty_endpoint is marked
# sensitive in variables.tf and must never be hardcoded. PagerDuty
# integration URLs are HTTPS POST endpoints provided by PagerDuty's
# CloudWatch integration; they embed an integration key that grants alert
# delivery to the configured PagerDuty service.
#
# endpoint_auto_confirms = true: PagerDuty's HTTPS integration responds to
# AWS's confirmation request automatically. Without this flag, the
# subscription stays PendingConfirmation indefinitely and no alerts are
# delivered.
###############################################################################

resource "aws_sns_topic_subscription" "pagerduty" {
  count = var.enable_pagerduty && var.pagerduty_endpoint != "" ? 1 : 0

  topic_arn              = aws_sns_topic.alarms.arn
  protocol               = "https"
  endpoint               = var.pagerduty_endpoint
  endpoint_auto_confirms = true
}

###############################################################################
# Log metric filters: extract custom CloudWatch metrics from the structured
# JSON logs emitted by structlog (backend/app/observability/logging.py).
#
# The backend emits log lines with this shape:
#   {
#     "level": "info",
#     "event": "ai_call_completed",
#     "ai_call_duration_ms": 4523,
#     "correlation_id": "..."
#   }
#
# The metric filter pattern matches the event field and extracts the
# duration field, publishing it as a CloudWatch metric in the
# local.metric_namespace namespace ("SalesConnections/<env>"). These
# filters power the P95 alarms downstream.
#
# CloudWatch's JSON pattern syntax:
#   - $.field             path expression for a top-level field
#   - = "literal"         equality check against a string literal
#   - = *                 matches any value (presence check)
#   - && / ||             boolean composition
###############################################################################

resource "aws_cloudwatch_log_metric_filter" "ai_latency" {
  name           = "${var.name_prefix}-ai-latency"
  log_group_name = aws_cloudwatch_log_group.backend.name

  pattern = "{ ($.event = \"ai_call_completed\") && ($.ai_call_duration_ms = *) }"

  metric_transformation {
    name          = "AICallDurationMs"
    namespace     = local.metric_namespace
    value         = "$.ai_call_duration_ms"
    default_value = null
    unit          = "Milliseconds"
  }
}

resource "aws_cloudwatch_log_metric_filter" "form_submit_latency" {
  name           = "${var.name_prefix}-form-submit-latency"
  log_group_name = aws_cloudwatch_log_group.backend.name

  pattern = "{ ($.event = \"connection_create_completed\") && ($.handler_duration_ms = *) }"

  metric_transformation {
    name          = "FormSubmitDurationMs"
    namespace     = local.metric_namespace
    value         = "$.handler_duration_ms"
    default_value = null
    unit          = "Milliseconds"
  }
}

resource "aws_cloudwatch_log_metric_filter" "audit_emit_latency" {
  name           = "${var.name_prefix}-audit-emit-latency"
  log_group_name = aws_cloudwatch_log_group.backend.name

  pattern = "{ ($.event = \"audit_event_emitted\") && ($.audit_emit_duration_ms = *) }"

  metric_transformation {
    name          = "AuditEmitDurationMs"
    namespace     = local.metric_namespace
    value         = "$.audit_emit_duration_ms"
    default_value = null
    unit          = "Milliseconds"
  }
}

###############################################################################
# Alarm 1: AI note generation P95 latency
#
# Budget per AAP Sec 0.7.3: AI note generation P95 <= 5000 ms end-to-end.
# Source metric: AICallDurationMs in local.metric_namespace, derived from
# the ai_latency log filter above.
#
# Trigger logic: 3-of-5 datapoints exceed the threshold across 5 evaluation
# periods of 5 minutes (so 3 breaches in 25 minutes triggers the alarm).
# extended_statistic = "p95" computes the 95th percentile across the period.
# treat_missing_data = "notBreaching": no AI calls = healthy state, since
# the F-002 endpoint is non-blocking and may receive no traffic for hours
# in low-volume environments.
###############################################################################

resource "aws_cloudwatch_metric_alarm" "ai_latency_p95" {
  alarm_name          = "${var.name_prefix}-ai-latency-p95"
  alarm_description   = "AI note generation P95 latency exceeded ${var.alarm_threshold_p95_ms_ai_call}ms (AAP Sec 0.7.3 budget: 5000ms)."
  comparison_operator = "GreaterThanThreshold"
  evaluation_periods  = 5
  datapoints_to_alarm = 3

  metric_name        = "AICallDurationMs"
  namespace          = local.metric_namespace
  period             = 300
  extended_statistic = "p95"
  threshold          = var.alarm_threshold_p95_ms_ai_call
  treat_missing_data = "notBreaching"

  alarm_actions = local.alarm_default_actions
  ok_actions    = local.alarm_ok_actions

  tags = merge(local.module_tags, {
    Name          = "${var.name_prefix}-ai-latency-p95"
    AlarmPriority = "High"
    Budget        = "AI-5s"
  })
}

###############################################################################
# Alarm 2: Form submit P95 latency
#
# Budget per AAP Sec 0.7.3: Form submit (excluding the AI call) P95 <= 2000
# ms. The "excluding AI" qualifier matters: the AI call is its own endpoint
# (POST /api/notes/generate) and its own alarm above; the form-submit
# alarm watches POST /api/connections, which is a database write plus an
# audit emit only.
#
# Same 3-of-5 trigger logic and notBreaching missing-data treatment as the
# AI alarm; same SNS fan-out.
###############################################################################

resource "aws_cloudwatch_metric_alarm" "form_submit_p95" {
  alarm_name          = "${var.name_prefix}-form-submit-p95"
  alarm_description   = "Form submit P95 latency exceeded ${var.alarm_threshold_p95_ms_form_submit}ms (AAP Sec 0.7.3 budget: 2000ms; AI excluded)."
  comparison_operator = "GreaterThanThreshold"
  evaluation_periods  = 5
  datapoints_to_alarm = 3

  metric_name        = "FormSubmitDurationMs"
  namespace          = local.metric_namespace
  period             = 300
  extended_statistic = "p95"
  threshold          = var.alarm_threshold_p95_ms_form_submit
  treat_missing_data = "notBreaching"

  alarm_actions = local.alarm_default_actions
  ok_actions    = local.alarm_ok_actions

  tags = merge(local.module_tags, {
    Name          = "${var.name_prefix}-form-submit-p95"
    AlarmPriority = "High"
    Budget        = "FormSubmit-2s"
  })
}

###############################################################################
# Alarm 3: Audit event emission P95 latency
#
# Budget per AAP Sec 0.7.3: Audit event emission P95 <= 100 ms. The audit
# emit happens inside the parent state-change transaction (per the
# atomic state-change + audit-emit invariant in AAP Sec 0.7.1), so a
# regression here directly inflates F-001/F-005/F-007 latency.
#
# Same 3-of-5 trigger logic; notBreaching for low-traffic windows.
###############################################################################

resource "aws_cloudwatch_metric_alarm" "audit_emit_p95" {
  alarm_name          = "${var.name_prefix}-audit-emit-p95"
  alarm_description   = "Audit event emission P95 latency exceeded ${var.alarm_threshold_audit_emit_ms}ms (AAP Sec 0.7.3 budget: 100ms; emitted in same transaction as state change)."
  comparison_operator = "GreaterThanThreshold"
  evaluation_periods  = 5
  datapoints_to_alarm = 3

  metric_name        = "AuditEmitDurationMs"
  namespace          = local.metric_namespace
  period             = 300
  extended_statistic = "p95"
  threshold          = var.alarm_threshold_audit_emit_ms
  treat_missing_data = "notBreaching"

  alarm_actions = local.alarm_default_actions
  ok_actions    = local.alarm_ok_actions

  tags = merge(local.module_tags, {
    Name          = "${var.name_prefix}-audit-emit-p95"
    AlarmPriority = "High"
    Budget        = "AuditEmit-100ms"
  })
}

###############################################################################
# Alarm 4: ALB target 5xx error rate (5-minute window)
#
# AWS/ApplicationELB.HTTPCode_Target_5XX_Count counts only target-side
# (backend Flask) 5xx responses, not ALB-side errors (which are
# HTTPCode_ELB_5XX_Count). Alarming on target 5xx is the right signal for
# backend health: ALB-side 5xx are usually configuration or capacity
# problems handled by the alb_target_unhealthy alarm below.
#
# Trigger: Sum of 5xx over a 5-minute window > var.alarm_threshold_5xx_per_5min.
# Single evaluation period (1-of-1) gives the fastest possible alert when
# the backend starts returning errors at scale.
###############################################################################

resource "aws_cloudwatch_metric_alarm" "alb_5xx_rate" {
  alarm_name          = "${var.name_prefix}-alb-5xx-rate"
  alarm_description   = "ALB target 5xx errors exceeded ${var.alarm_threshold_5xx_per_5min} in a 5-minute window."
  comparison_operator = "GreaterThanThreshold"
  evaluation_periods  = 1
  datapoints_to_alarm = 1

  metric_name        = "HTTPCode_Target_5XX_Count"
  namespace          = "AWS/ApplicationELB"
  period             = 300
  statistic          = "Sum"
  threshold          = var.alarm_threshold_5xx_per_5min
  treat_missing_data = "notBreaching"

  dimensions = {
    LoadBalancer = var.alb_arn_suffix
  }

  alarm_actions = local.alarm_default_actions
  ok_actions    = local.alarm_ok_actions

  tags = merge(local.module_tags, {
    Name          = "${var.name_prefix}-alb-5xx-rate"
    AlarmPriority = "High"
  })
}

###############################################################################
# Alarm 5: ALB unhealthy target hosts > 0
#
# Any unhealthy target host is a problem worth paging on, so threshold is
# 0. evaluation_periods=2 with period=60 means two consecutive 1-minute
# windows must report an unhealthy host before the alarm fires; this
# debounces transient health-check blips during deploys (where a task is
# briefly unhealthy as it transitions between Draining and Stopped).
#
# Both TargetGroup and LoadBalancer dimensions are required for the
# UnHealthyHostCount metric in AWS/ApplicationELB.
###############################################################################

resource "aws_cloudwatch_metric_alarm" "alb_target_unhealthy" {
  alarm_name          = "${var.name_prefix}-alb-target-unhealthy"
  alarm_description   = "One or more ALB target hosts reported as unhealthy by the target group health check."
  comparison_operator = "GreaterThanThreshold"
  evaluation_periods  = 2
  datapoints_to_alarm = 2

  metric_name        = "UnHealthyHostCount"
  namespace          = "AWS/ApplicationELB"
  period             = 60
  statistic          = "Maximum"
  threshold          = 0
  treat_missing_data = "notBreaching"

  dimensions = {
    TargetGroup  = var.alb_target_group_arn_suffix
    LoadBalancer = var.alb_arn_suffix
  }

  alarm_actions = local.alarm_default_actions
  ok_actions    = local.alarm_ok_actions

  tags = merge(local.module_tags, {
    Name          = "${var.name_prefix}-alb-target-unhealthy"
    AlarmPriority = "High"
  })
}

###############################################################################
# Alarm 6: ECS running task count below desired
#
# Detects scaling failures: insufficient Fargate capacity, image pull
# errors, container crashes during startup, or health-check failures that
# prevent tasks from reaching Running state.
#
# Threshold = var.ecs_service_desired_count: alarm fires when the
# RunningTaskCount average drops below the desired count. Note the
# LessThanThreshold comparison (not GreaterThanThreshold like the other
# alarms).
#
# treat_missing_data = "breaching": absence of the metric is itself a
# problem (Container Insights stopped reporting = service down). This is
# the only alarm in the module that treats missing data as breaching;
# every other alarm treats absence as healthy because no traffic = no
# problem.
#
# ECS/ContainerInsights namespace requires Container Insights enabled on
# the cluster (set in modules/ecs/).
###############################################################################

resource "aws_cloudwatch_metric_alarm" "ecs_running_task_count" {
  alarm_name          = "${var.name_prefix}-ecs-running-task-count"
  alarm_description   = "ECS service running task count is below the desired count of ${var.ecs_service_desired_count}."
  comparison_operator = "LessThanThreshold"
  evaluation_periods  = 3
  datapoints_to_alarm = 2

  metric_name        = "RunningTaskCount"
  namespace          = "ECS/ContainerInsights"
  period             = 60
  statistic          = "Average"
  threshold          = var.ecs_service_desired_count
  treat_missing_data = "breaching"

  dimensions = {
    ClusterName = var.ecs_cluster_name
    ServiceName = var.ecs_service_name
  }

  alarm_actions = local.alarm_default_actions
  ok_actions    = local.alarm_ok_actions

  tags = merge(local.module_tags, {
    Name          = "${var.name_prefix}-ecs-running-task-count"
    AlarmPriority = "High"
  })
}

###############################################################################
# Alarm 7: RDS CPU utilization sustained above 80%
#
# 3 consecutive 5-minute windows above 80% CPU triggers; debounces brief
# spikes during query bursts that are normal for a transactional workload.
# Sustained 80%+ usually indicates either (a) a missing index on a hot
# query, (b) a need for a larger instance class, or (c) an external
# pathological workload (e.g., misbehaving cron job).
#
# Priority: Medium (vs the other High-priority alarms) because RDS Multi-AZ
# can absorb significant CPU pressure before user-visible latency degrades;
# the operator response is to investigate before scale-up, not to page on
# call.
###############################################################################

resource "aws_cloudwatch_metric_alarm" "rds_cpu" {
  alarm_name          = "${var.name_prefix}-rds-cpu"
  alarm_description   = "RDS CPU utilization sustained above 80% over 15 minutes."
  comparison_operator = "GreaterThanThreshold"
  evaluation_periods  = 3
  datapoints_to_alarm = 3

  metric_name        = "CPUUtilization"
  namespace          = "AWS/RDS"
  period             = 300
  statistic          = "Average"
  threshold          = 80
  treat_missing_data = "notBreaching"

  dimensions = {
    DBInstanceIdentifier = var.rds_instance_id
  }

  alarm_actions = local.alarm_default_actions
  ok_actions    = local.alarm_ok_actions

  tags = merge(local.module_tags, {
    Name          = "${var.name_prefix}-rds-cpu"
    AlarmPriority = "Medium"
  })
}

###############################################################################
# Alarm 8: RDS free storage space below the configured floor
#
# Threshold expressed in BYTES via var.alarm_threshold_rds_free_storage_bytes
# (default 10 GiB). Operators tune this to ~20% of allocated storage so the
# alarm fires with enough lead time for storage scale-up (which can take
# 10-15 minutes on RDS) before the database hits the wall.
#
# Single evaluation period (1-of-1) for the fastest possible alert: storage
# exhaustion is one of the few RDS failure modes that is hard-irrecoverable
# at the data level (transactions queue and eventually fail).
###############################################################################

resource "aws_cloudwatch_metric_alarm" "rds_storage_free" {
  alarm_name          = "${var.name_prefix}-rds-storage-free"
  alarm_description   = "RDS free storage space dropped below ${var.alarm_threshold_rds_free_storage_bytes} bytes (configured floor)."
  comparison_operator = "LessThanThreshold"
  evaluation_periods  = 1
  datapoints_to_alarm = 1

  metric_name        = "FreeStorageSpace"
  namespace          = "AWS/RDS"
  period             = 300
  statistic          = "Average"
  threshold          = var.alarm_threshold_rds_free_storage_bytes
  treat_missing_data = "notBreaching"

  dimensions = {
    DBInstanceIdentifier = var.rds_instance_id
  }

  alarm_actions = local.alarm_default_actions
  ok_actions    = local.alarm_ok_actions

  tags = merge(local.module_tags, {
    Name          = "${var.name_prefix}-rds-storage-free"
    AlarmPriority = "High"
  })
}

