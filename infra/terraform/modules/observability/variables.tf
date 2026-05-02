###############################################################################
# infra/terraform/modules/observability/variables.tf
#
# Input variables for the Sales-Connections observability module.
#
# Variable groups (in declaration order below):
#   1. Identity / naming                  : name_prefix, environment, region
#   2. CloudWatch retention / encryption  : log_retention_days, kms_key_id
#   3. Service identifiers                : backend_service_name,
#                                           frontend_service_name
#   4. ECS / RDS / ALB ARNs (alarm dims)  : ecs_cluster_name, ecs_service_name,
#                                           ecs_service_desired_count,
#                                           rds_instance_id, alb_arn_suffix,
#                                           alb_target_group_arn_suffix
#   5. Alarm subscribers                  : alarm_email_addresses,
#                                           enable_pagerduty,
#                                           pagerduty_endpoint
#   6. Alarm thresholds (per AAP Sec 0.7.3): alarm_threshold_p95_ms_form_submit,
#                                            alarm_threshold_p95_ms_ai_call,
#                                            alarm_threshold_5xx_per_5min,
#                                            alarm_threshold_audit_emit_ms,
#                                            alarm_threshold_rds_free_storage_bytes
#   7. ALB log bucket tuning              : alb_access_logs_to_cloudwatch,
#                                           alb_log_bucket_force_destroy
#   8. Dashboard extension                : dashboard_widgets_extra
#   9. Tagging                            : tags
#
# Validation rules:
#   - name_prefix:           lowercase + digits + hyphens, length-bounded
#   - environment:           dev / staging / prod
#   - region:                non-empty AWS region pattern
#   - log_retention_days:    one of CloudWatch's enumerated retention values
#   - kms_key_id:            empty OR valid KMS key ID/ARN/alias pattern
#   - alarm_email_addresses: each entry a valid email pattern
#   - pagerduty_endpoint:    empty OR valid HTTPS URL
#   - threshold variables:   numeric > 0
#
# Default-value policy (per folder spec critical constraints):
#   - kms_key_id:                              ""    (AWS-managed key acceptable for MVP)
#   - frontend_service_name:                   ""    (static delivery default)
#   - ecs_service_desired_count:               2     (typical HA Fargate config)
#   - alarm_email_addresses:                   []    (subscribers added per env)
#   - enable_pagerduty:                        false (PagerDuty optional)
#   - pagerduty_endpoint:                      ""    (sensitive; sourced from secrets)
#   - alarm_threshold_p95_ms_form_submit:      2000  (per AAP Sec 0.7.3)
#   - alarm_threshold_p95_ms_ai_call:          5000  (per AAP Sec 0.7.3)
#   - alarm_threshold_5xx_per_5min:            10
#   - alarm_threshold_audit_emit_ms:           100   (per AAP Sec 0.7.3)
#   - alarm_threshold_rds_free_storage_bytes:  10737418240  (10 GiB; trips before tier upgrade)
#   - alb_access_logs_to_cloudwatch:           false (opt-in S3 to CW subscription)
#   - alb_log_bucket_force_destroy:            false (production safety)
#   - dashboard_widgets_extra:                 []    (no extra widgets)
#   - tags:                                    {}    (caller layer)
#
# Cross-variable validation note:
#   Terraform 1.7 (the project's pinned floor per versions.tf) does not allow
#   cross-variable references inside a variable's validation block. The pair
#   (enable_pagerduty == true => pagerduty_endpoint must be non-empty HTTPS URL)
#   is enforced via lifecycle.precondition on the aws_sns_topic_subscription
#   resource in main.tf rather than at the variable-validation layer.
###############################################################################

###############################################################################
# 1. Identity and naming
###############################################################################

variable "name_prefix" {
  description = "Resource name prefix used in tags, Name labels, and resource names (e.g., 'sales-connections-prod'). Composed by the parent as '$${var.project}-$${var.environment}'. Used to name CloudWatch log groups (/ecs/$${name_prefix}/backend), the SNS topic ($${name_prefix}-alarms), the S3 bucket ($${name_prefix}-alb-logs), and every alarm ($${name_prefix}-<alarm-name>)."
  type        = string

  validation {
    condition     = length(var.name_prefix) > 0 && length(var.name_prefix) <= 50
    error_message = "name_prefix must be a non-empty string of at most 50 characters (S3 bucket names cap at 63; we leave 13 chars for the '-alb-logs' suffix and any future expansions)."
  }

  validation {
    condition     = can(regex("^[a-z][a-z0-9-]*[a-z0-9]$", var.name_prefix))
    error_message = "name_prefix must start with a lowercase letter, end with a letter or digit, and contain only lowercase letters, digits, and hyphens (S3-bucket-name-safe)."
  }
}

variable "environment" {
  description = "Environment label (one of: dev, staging, prod). Used as the Environment tag value for cost-explorer drill-down, embedded in the custom CloudWatch metric namespace (SalesConnections/<env>), and surfaced in the dashboard text widget."
  type        = string

  validation {
    condition     = contains(["dev", "staging", "prod"], var.environment)
    error_message = "environment must be one of: dev, staging, prod."
  }
}

variable "region" {
  description = "AWS region for this deployment (e.g., 'us-east-1'). Used to construct the CloudWatch dashboard console URL and as the explicit region argument on every dashboard widget so the dashboard renders correctly when displayed cross-region."
  type        = string

  validation {
    condition     = can(regex("^[a-z]{2}(-gov)?-[a-z]+-[0-9]+$", var.region))
    error_message = "region must be a valid AWS region pattern (e.g., 'us-east-1', 'eu-west-2', 'us-gov-west-1')."
  }
}

###############################################################################
# 2. CloudWatch retention and encryption
###############################################################################

variable "log_retention_days" {
  description = "Number of days CloudWatch Logs retains entries. MUST be one of CloudWatch's enumerated retention values (1, 3, 5, 7, 14, 30, 60, 90, 120, 150, 180, 365, 400, 545, 731, 1827, 2192, 2557, 2922, 3288, 3653). Per the folder spec, NEVER null (which means 'never expire' and would balloon costs)."
  type        = number

  validation {
    condition = contains(
      [1, 3, 5, 7, 14, 30, 60, 90, 120, 150, 180, 365, 400, 545, 731, 1827, 2192, 2557, 2922, 3288, 3653],
      var.log_retention_days,
    )
    error_message = "log_retention_days must be one of CloudWatch's enumerated values: 1, 3, 5, 7, 14, 30, 60, 90, 120, 150, 180, 365, 400, 545, 731, 1827, 2192, 2557, 2922, 3288, 3653. Per folder spec, never null/0 (which would mean 'never expire')."
  }
}

variable "kms_key_id" {
  description = "Optional KMS key ID, ARN, or alias for encrypting CloudWatch log groups, the SNS topic, and the ALB logs S3 bucket. Empty string (default) uses AWS-managed keys: alias/aws/logs for log groups, alias/aws/sns for SNS, AES256 for S3 (acceptable for MVP per AAP Sec 0.4.9). Set to a customer-managed CMK ARN to override across all three services."
  type        = string
  default     = ""
}

###############################################################################
# 3. Service identifiers
###############################################################################

variable "backend_service_name" {
  description = "Service identifier for the backend container (e.g., 'sales-connections-prod-backend'). Surfaced in dashboard widget titles and alarm tags. Typically constructed by the root composition as '$${name_prefix}-backend'."
  type        = string
}

variable "frontend_service_name" {
  description = "Service identifier for the optional frontend container. Empty string when frontend_delivery_mode == 'static' (S3+CloudFront delivery with no ECS frontend). Used in dashboard widget titles when populated. Default empty supports the typical static-delivery configuration."
  type        = string
  default     = ""
}

###############################################################################
# 4. ECS / RDS / ALB identifiers used as alarm metric dimensions
#
# These come from sibling modules (ecs, database, alb) at apply time.
# Whether they are passed as direct module references or as constructed
# strings depends on the root composition's dependency graph (see
# infra/terraform/main.tf). The observability module accepts them as
# strings either way.
###############################################################################

variable "ecs_cluster_name" {
  description = "ECS cluster name used as the ClusterName dimension on AWS/ECS and ECS/ContainerInsights metric alarms (and dashboard widgets). Typically '$${name_prefix}-cluster' or sourced from module.ecs.cluster_name."
  type        = string

  validation {
    condition     = length(var.ecs_cluster_name) > 0
    error_message = "ecs_cluster_name must be non-empty (ECS metric alarms require this dimension)."
  }
}

variable "ecs_service_name" {
  description = "ECS service name used as the ServiceName dimension on AWS/ECS and ECS/ContainerInsights metric alarms. Typically '$${name_prefix}-backend' or sourced from module.ecs.service_name."
  type        = string

  validation {
    condition     = length(var.ecs_service_name) > 0
    error_message = "ecs_service_name must be non-empty (ECS metric alarms require this dimension)."
  }
}

variable "ecs_service_desired_count" {
  description = "Desired Fargate task count for the ECS service. Used as the threshold for the 'running task count' alarm: the alarm fires when the running count is BELOW this value. Default 2 matches the typical HA Fargate configuration (one task per AZ); production environments may override to 3+ for higher availability."
  type        = number
  default     = 2

  validation {
    condition     = var.ecs_service_desired_count >= 1
    error_message = "ecs_service_desired_count must be at least 1."
  }
}

variable "rds_instance_id" {
  description = "RDS DB instance identifier used as the DBInstanceIdentifier dimension on AWS/RDS metric alarms. Typically sourced from module.database.instance_id."
  type        = string

  validation {
    condition     = length(var.rds_instance_id) > 0
    error_message = "rds_instance_id must be non-empty (RDS metric alarms require this dimension)."
  }
}

variable "alb_arn_suffix" {
  description = "Application Load Balancer ARN suffix (e.g., 'app/sales-connections-prod-alb/abc123def456') used as the LoadBalancer dimension on AWS/ApplicationELB metrics. Sourced from module.alb.arn_suffix."
  type        = string

  validation {
    condition     = length(var.alb_arn_suffix) > 0
    error_message = "alb_arn_suffix must be non-empty (ALB metric alarms require this dimension)."
  }
}

variable "alb_target_group_arn_suffix" {
  description = "Target group ARN suffix (e.g., 'targetgroup/sales-connections-prod-tg/xyz789') used as the TargetGroup dimension on AWS/ApplicationELB target-health alarms. Sourced from module.alb.target_group_arn_suffix."
  type        = string

  validation {
    condition     = length(var.alb_target_group_arn_suffix) > 0
    error_message = "alb_target_group_arn_suffix must be non-empty (ALB target-health alarms require this dimension)."
  }
}

###############################################################################
# 5. Alarm subscribers: email and PagerDuty
###############################################################################

variable "alarm_email_addresses" {
  description = "List of email addresses subscribed to the SNS alarm topic. AWS sends a confirmation email to each; subscriptions remain pending until the recipient confirms. Default empty list means no email subscribers (typical for dev environments). Production environments should supply the on-call alias."
  type        = list(string)
  default     = []

  validation {
    condition = alltrue([
      for addr in var.alarm_email_addresses :
      can(regex("^[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\\.[A-Za-z]{2,}$", addr))
    ])
    error_message = "Every entry in alarm_email_addresses must be a valid email address (RFC 5322-ish: local-part@domain.tld)."
  }
}

variable "enable_pagerduty" {
  description = "Whether to subscribe the PagerDuty CloudWatch integration to the SNS alarm topic. When true, var.pagerduty_endpoint must be a valid HTTPS URL. Default false: PagerDuty integration is opt-in (typical for staging/prod)."
  type        = bool
  default     = false
}

variable "pagerduty_endpoint" {
  description = "PagerDuty CloudWatch integration HTTPS endpoint URL. Required when var.enable_pagerduty == true; ignored otherwise. SENSITIVE: this URL embeds an integration key that grants alert delivery to the PagerDuty service. Sourced from AWS Secrets Manager (or environment variables passed via terraform.tfvars), NEVER hardcoded in source files."
  type        = string
  default     = ""
  sensitive   = true

  validation {
    condition     = var.pagerduty_endpoint == "" || can(regex("^https://", var.pagerduty_endpoint))
    error_message = "pagerduty_endpoint must be empty or a valid HTTPS URL."
  }
}

###############################################################################
# 6. Alarm thresholds (per AAP Sec 0.7.3 performance budgets)
###############################################################################

variable "alarm_threshold_p95_ms_form_submit" {
  description = "P95 latency threshold (in milliseconds) for the form-submit alarm. Default 2000 matches AAP Sec 0.7.3 ('Form submit (excluding AI) <= 2 s'). When P95 of FormSubmitDurationMs metric (derived from log filter) exceeds this for 3 of 5 datapoints, the form_submit_p95 alarm fires."
  type        = number
  default     = 2000

  validation {
    condition     = var.alarm_threshold_p95_ms_form_submit > 0
    error_message = "alarm_threshold_p95_ms_form_submit must be greater than 0."
  }
}

variable "alarm_threshold_p95_ms_ai_call" {
  description = "P95 latency threshold (in milliseconds) for the AI-call alarm. Default 5000 matches AAP Sec 0.7.3 ('AI note generation <= 5 s P95 end-to-end'). When P95 of AICallDurationMs (derived from log filter) exceeds this for 3 of 5 datapoints, the ai_latency_p95 alarm fires."
  type        = number
  default     = 5000

  validation {
    condition     = var.alarm_threshold_p95_ms_ai_call > 0
    error_message = "alarm_threshold_p95_ms_ai_call must be greater than 0."
  }
}

variable "alarm_threshold_5xx_per_5min" {
  description = "Number of HTTPCode_Target_5XX_Count events in a 5-minute window above which the alb_5xx_rate alarm fires. Default 10. Tuned higher for low-traffic dev environments and lower for stable prod."
  type        = number
  default     = 10

  validation {
    condition     = var.alarm_threshold_5xx_per_5min > 0
    error_message = "alarm_threshold_5xx_per_5min must be greater than 0."
  }
}

variable "alarm_threshold_audit_emit_ms" {
  description = "P95 latency threshold (in milliseconds) for the audit emission alarm. Default 100 matches AAP Sec 0.7.3 ('Audit event emission <= 100 ms; emitted in same transaction as state change'). When P95 of AuditEmitDurationMs exceeds this for 3 of 5 datapoints, the audit_emit_p95 alarm fires."
  type        = number
  default     = 100

  validation {
    condition     = var.alarm_threshold_audit_emit_ms > 0
    error_message = "alarm_threshold_audit_emit_ms must be greater than 0."
  }
}

variable "alarm_threshold_rds_free_storage_bytes" {
  description = "Free storage space threshold in BYTES below which the rds_storage_free alarm fires. Default 10737418240 (10 GiB). Production with larger DBs should override this to ~20% of allocated storage."
  type        = number
  default     = 10737418240 # 10 GiB

  validation {
    condition     = var.alarm_threshold_rds_free_storage_bytes > 0
    error_message = "alarm_threshold_rds_free_storage_bytes must be greater than 0 (typically several GB to allow time for storage scale-up before the DB hits the wall)."
  }
}

###############################################################################
# 7. ALB access log bucket tuning
###############################################################################

variable "alb_access_logs_to_cloudwatch" {
  description = "Whether to provision a CloudWatch log group that mirrors ALB access logs from S3 via subscription. Default false: logs live only in S3 and are queried via Athena. Set true to enable a CloudWatch sink for ad-hoc Insights queries (the S3 -> CW subscription Lambda is post-MVP and not provisioned by this module; only the destination log group)."
  type        = bool
  default     = false
}

variable "alb_log_bucket_force_destroy" {
  description = "Whether 'terraform destroy' may delete the ALB access logs S3 bucket even when objects are present. Set true ONLY in dev for clean tear-down; false in prod so accidental destroy fails with a clear error (defense against operator mistakes)."
  type        = bool
  default     = false
}

###############################################################################
# 8. Dashboard extension: extra widgets appended to the bottom of the layout
###############################################################################

variable "dashboard_widgets_extra" {
  description = "List of extra CloudWatch dashboard widget JSON objects appended at the bottom of the dashboard. Each entry must conform to the CloudWatch Dashboard Body Structure (see AWS docs at AmazonCloudWatch/latest/APIReference/CloudWatch-Dashboard-Body-Structure.html). Default empty list. Useful for layering in environment-specific or per-tenant widgets without forking this module."
  type        = list(any)
  default     = []
}

###############################################################################
# 9. Tagging
###############################################################################

variable "tags" {
  description = "Map of tags merged into every resource created by this module. The module layers in 'Component = Observability', 'Environment = var.environment', 'ManagedBy = Terraform', and 'Module = infra/terraform/modules/observability' on top of these (in locals.tf). Per-resource labels (Name, Tier, AlarmPriority, Budget) are also layered at the resource site. The parent composition's provider-level default_tags also apply via AWS provider 5.x default_tags."
  type        = map(string)
  default     = {}
}
