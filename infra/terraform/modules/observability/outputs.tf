###############################################################################
# infra/terraform/modules/observability/outputs.tf
#
# Outputs surfaced by the observability module. Consumed by:
#   - infra/terraform/main.tf (root composition module wiring)
#   - infra/terraform/outputs.tf (re-exported as root composition outputs)
#   - infra/terraform/modules/ecs/  (backend_log_group_name -> awslogs driver)
#   - infra/terraform/modules/alb/  (alb_access_logs_bucket -> access_logs)
#   - external runbooks (sns_alarm_topic_arn -> add subscribers ad hoc)
#
# These outputs are the public CONTRACT of the observability module.
# Renaming any output here is a breaking change to the entire Terraform
# composition.
#
# Convention:
#   - Backend log group outputs reference unconditional resources (no count
#     guard); attributes are accessed directly without try() so that any
#     legitimate provisioning failure surfaces loudly instead of silently
#     emitting an empty string.
#   - ALB log group output uses try(...) so it returns "" when the optional
#     CloudWatch sink isn't enabled (var.alb_access_logs_to_cloudwatch ==
#     false). Without try(), referencing aws_cloudwatch_log_group.alb_access_logs[0]
#     when count = 0 would error at plan time.
#   - S3 bucket outputs are unconditional (the bucket is always created so
#     the ALB module can wire it without a circular dependency on the ALB's
#     own access_logs_enabled toggle; the ALB module governs whether logs
#     are actually written).
#   - SNS topic and dashboard outputs are unconditional.
#   - Every output declares description (mandatory per the folder spec
#     Authoring Conventions).
#   - No `sensitive = true` markers: log group names, ARNs, bucket names,
#     dashboard names, console URLs, and SNS topic ARNs are NOT secrets.
#     They appear in CloudTrail, IAM policies, runbook documentation, and
#     Terraform state. Per AAP Sec 0.7.4, only secret VALUES are sensitive
#     - these are public infrastructure identifiers.
###############################################################################

###############################################################################
# Backend CloudWatch log group (always created; consumed by ECS module)
#
# The ECS task definition's awslogs driver references this log group by NAME
# (a string), so the ECS module reads backend_log_group_name and writes:
#   logConfiguration {
#     logDriver = "awslogs"
#     options = {
#       awslogs-group         = var.backend_log_group_name
#       awslogs-region        = var.region
#       awslogs-stream-prefix = "ecs"
#     }
#   }
###############################################################################

output "backend_log_group_name" {
  description = "Name of the CloudWatch log group receiving Flask/Gunicorn stdout from the backend ECS task. Consumed by modules/ecs/ for the awslogs driver configuration in the task definition. Re-exported at root as cloudwatch_log_group_backend for runbook references."
  value       = aws_cloudwatch_log_group.backend.name
}

output "backend_log_group_arn" {
  description = "ARN of the backend CloudWatch log group. Used by IAM policy resource scoping (e.g., ECS task execution role's logs:CreateLogStream and logs:PutLogEvents permissions can be scoped to this exact log group rather than logs:* on a wildcard)."
  value       = aws_cloudwatch_log_group.backend.arn
}

###############################################################################
# ALB CloudWatch log group (conditional; created only when
# var.alb_access_logs_to_cloudwatch == true)
#
# The S3 -> CloudWatch subscription is the optional pipeline that mirrors ALB
# access logs into a queryable CloudWatch group for ad-hoc Insights queries.
# When disabled (default false), these outputs return "" so downstream
# consumers can detect "no log group" without a null check (length() > 0
# style guards).
###############################################################################

output "alb_log_group_name" {
  description = "Name of the optional CloudWatch log group that mirrors ALB access logs (S3 to CloudWatch subscription). Returns empty string when var.alb_access_logs_to_cloudwatch == false. Re-exported at root as cloudwatch_log_group_alb."
  value       = try(aws_cloudwatch_log_group.alb_access_logs[0].name, "")
}

output "alb_log_group_arn" {
  description = "ARN of the optional ALB CloudWatch log group, or empty string when not provisioned. Useful for IAM policy resource scoping if a future log-processor Lambda is added."
  value       = try(aws_cloudwatch_log_group.alb_access_logs[0].arn, "")
}

###############################################################################
# S3 bucket for ALB access logs (always created)
#
# Consumed by modules/alb/ for the access_logs block on the
# aws_lb resource:
#   access_logs {
#     bucket  = var.access_logs_bucket
#     prefix  = "alb"
#     enabled = var.access_logs_enabled
#   }
#
# The bucket is always created so the ALB module can wire to it
# unconditionally; the ALB's own access_logs_enabled toggle governs whether
# logs are actually written. This separation prevents observability from
# needing to know the ALB's intentions (which would create a coupling).
###############################################################################

output "alb_access_logs_bucket" {
  description = "Name of the S3 bucket receiving ALB access logs. Consumed by modules/alb/ for the access_logs block on the ALB resource. The ALB module's own access_logs_enabled toggle governs whether logs are actually written; this bucket exists unconditionally so the ALB can wire to it without circular dependencies."
  value       = aws_s3_bucket.alb_access_logs.id
}

output "alb_access_logs_bucket_arn" {
  description = "Full ARN of the ALB access logs S3 bucket. Used in IAM policy resource scoping (e.g., a future log-processor Lambda's s3:GetObject permission can be scoped to this exact bucket)."
  value       = aws_s3_bucket.alb_access_logs.arn
}

output "alb_access_logs_bucket_domain_name" {
  description = "Regional domain name of the ALB access logs S3 bucket (e.g., 'sales-connections-prod-alb-logs.s3.us-east-1.amazonaws.com'). Useful for direct s3:// or https:// access patterns from operator tooling."
  value       = aws_s3_bucket.alb_access_logs.bucket_regional_domain_name
}

###############################################################################
# CloudWatch dashboard outputs
#
# The dashboard provides a single-pane-of-glass view of the four performance
# budgets (AAP Sec 0.7.3) plus standard SLI/SLO panels. Operators access it
# via dashboard_url, which is region-aware so the link lands on the correct
# dashboard regardless of the operator's current console region context.
###############################################################################

output "dashboard_name" {
  description = "Name of the main CloudWatch dashboard for Sales-Connections operations (e.g., 'sales-connections-prod'). Re-exported at root as cloudwatch_dashboard_name. Used by docs/operations.md and dashboard hyperlinks."
  value       = aws_cloudwatch_dashboard.main.dashboard_name
}

output "dashboard_url" {
  description = "Direct browser URL to the CloudWatch dashboard, region-aware (e.g., 'https://us-east-1.console.aws.amazon.com/cloudwatch/home?region=us-east-1#dashboards:name=sales-connections-prod'). Useful for pasting into runbooks, incident channels, and email alerts."
  value       = "https://${var.region}.console.aws.amazon.com/cloudwatch/home?region=${var.region}#dashboards:name=${aws_cloudwatch_dashboard.main.dashboard_name}"
}

###############################################################################
# SNS topic for alarm fan-out
#
# Per AAP Sec 0.4.9, this topic is for ALARM FAN-OUT ONLY. It must NEVER be
# used for application message passing - the project's synchronous-only
# integration stance forbids message queues, event buses, and pub/sub
# patterns for application traffic.
#
# Surfacing the ARN lets operators add subscribers ad-hoc (e.g., a Slack
# webhook via Lambda, a PagerDuty integration not configured at apply time)
# without re-running terraform apply.
###############################################################################

output "sns_alarm_topic_arn" {
  description = "ARN of the SNS topic that fans out CloudWatch alarms to subscribers (email subscriptions configured via var.alarm_email_addresses; PagerDuty subscription configured when var.enable_pagerduty == true). Surfaced for runbook scripts that subscribe additional consumers (e.g., a Slack webhook Lambda) without requiring terraform apply. Per AAP Sec 0.4.9 the topic is for alarm fan-out ONLY, never for application message passing."
  value       = aws_sns_topic.alarms.arn
}

output "sns_alarm_topic_name" {
  description = "Name of the SNS alarm topic (e.g., 'sales-connections-prod-alarms'). Useful for AWS console navigation and CLI operations."
  value       = aws_sns_topic.alarms.name
}
