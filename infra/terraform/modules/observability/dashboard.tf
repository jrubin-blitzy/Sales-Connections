###############################################################################
# infra/terraform/modules/observability/dashboard.tf
#
# Single CloudWatch dashboard for the Sales-Connections operations view.
#
# Widget layout (4 columns x 6 rows = 24 widgets max; we use ~17):
#
#   Row 1 (Performance budget headlines, single-stat):
#     [AI P95]              [Form P95]               [Audit P95]            [ALB 5xx]
#
#   Row 2 (ALB / request flow, time-series):
#     [Request count]       [Target response time]   [Healthy hosts]        [Unhealthy hosts]
#
#   Row 3 (ECS / compute, time-series):
#     [CPU utilization]     [Memory utilization]     [Running task count]   [Deployment count]
#
#   Row 4 (RDS / data tier, time-series):
#     [CPU]                 [Free storage]           [Connections]          [Read/Write latency]
#
#   Row 5 (Custom SLI / business metrics + text overview):
#     [AI call count]       [Form submit count]      [Audit emission count] [Text overview]
#
#   Row 6+ (Reserved for var.dashboard_widgets_extra appended at the bottom)
#
# All widgets reference metrics by namespace + dimensions; each carries an
# explicit region = var.region so the dashboard renders correctly when
# displayed cross-region. All time-series widgets share the 5-minute period
# (local.dashboard_default_period) so dashboard trends and alarm thresholds
# (also 5-minute windows) align temporally.
#
# JSON construction: jsonencode() over a Terraform local map. We do NOT
# use templatefile() because:
#   - jsonencode() validates structure at terraform plan time
#   - HCL syntax for nested objects is more readable than escaped JSON
#   - Parameter substitution (var.X, local.X) is type-checked
#   - terraform fmt canonicalizes indentation cleanly
#   - Easier review than 200+ lines of escaped JSON
#
# Reference: AWS docs - CloudWatch Dashboard Body Structure
#   https://docs.aws.amazon.com/AmazonCloudWatch/latest/APIReference/CloudWatch-Dashboard-Body-Structure.html
###############################################################################

locals {
  #############################################################################
  # Dashboard widget definitions (built within this file's locals so the
  # dashboard JSON stays adjacent to its construction logic).
  #
  # Note: HCL allows multiple `locals { }` blocks in different files; both
  # contribute to the same module-wide locals namespace. The locals defined
  # here coexist with those in locals.tf without redeclaration conflict.
  #
  # Each widget is a CloudWatch Dashboard Body Structure object. The
  # widgets are concatenated in row order (top-left to bottom-right).
  #
  # Widget grid: 24 columns x 1000 rows. Each row is height 6 by convention;
  # widgets in the same row share y; x increases across columns
  # (0, 6, 12, 18) to fill the 24-column width with four 6-wide widgets.
  #
  # Standard sizes used here:
  #   Single-stat headline:  width 6, height 6, type "metric", view "singleValue"
  #   Time-series chart:     width 6, height 6, type "metric", view "timeSeries"
  #   Text/markdown overview: width 6, height 6, type "text"
  #############################################################################

  # Common period (5 minutes = 300 seconds) for all time-series widgets.
  # Matches the alarm evaluation period in main.tf so dashboard trends and
  # alarm thresholds align temporally.
  dashboard_default_period = 300

  # Helper: ALB metric dimensions used by multiple widgets. Kept as a
  # named local for readability; the dimension list is inlined in each
  # widget below because the AWS metric format is positional.
  dashboard_alb_dimensions = {
    LoadBalancer = var.alb_arn_suffix
  }

  # Helper: ALB target group dimensions used for target-health widgets.
  dashboard_alb_tg_dimensions = {
    LoadBalancer = var.alb_arn_suffix
    TargetGroup  = var.alb_target_group_arn_suffix
  }

  # Helper: ECS service dimensions.
  dashboard_ecs_dimensions = {
    ClusterName = var.ecs_cluster_name
    ServiceName = var.ecs_service_name
  }

  # Helper: RDS instance dimensions.
  dashboard_rds_dimensions = {
    DBInstanceIdentifier = var.rds_instance_id
  }

  #############################################################################
  # Row 1 - Performance budget headlines (single-stat metric widgets)
  #
  # Each widget renders a single P95 latency value with a red horizontal
  # threshold annotation aligned to the AAP Sec 0.7.3 budgets. This is the
  # at-a-glance view operators see first when opening the dashboard.
  #
  # The four headlines are:
  #   [0,0]  AI note generation P95  (budget 5000 ms)
  #   [6,0]  Form submit P95         (budget 2000 ms)
  #   [12,0] Audit emit P95          (budget 100 ms)
  #   [18,0] ALB 5xx target errors   (threshold from var.alarm_threshold_5xx_per_5min)
  #
  # Custom metrics (AI/Form/Audit) are emitted by log metric filters in
  # main.tf and live under local.metric_namespace ("SalesConnections/<env>").
  # ALB 5xx metric is from the AWS-managed AWS/ApplicationELB namespace.
  #############################################################################
  dashboard_widgets_row1 = [
    {
      type   = "metric"
      x      = 0
      y      = 0
      width  = 6
      height = 6
      properties = {
        title  = "AI note generation P95 (budget: 5s)"
        view   = "singleValue"
        region = var.region
        metrics = [
          [local.metric_namespace, "AICallDurationMs", { stat = "p95", period = local.dashboard_default_period, label = "AI P95 (ms)" }]
        ]
        annotations = {
          horizontal = [
            { value = var.alarm_threshold_p95_ms_ai_call, label = "AI budget (${var.alarm_threshold_p95_ms_ai_call}ms)", color = "#d62728" }
          ]
        }
        yAxis = {
          left = { min = 0 }
        }
      }
    },
    {
      type   = "metric"
      x      = 6
      y      = 0
      width  = 6
      height = 6
      properties = {
        title  = "Form submit P95 (budget: 2s)"
        view   = "singleValue"
        region = var.region
        metrics = [
          [local.metric_namespace, "FormSubmitDurationMs", { stat = "p95", period = local.dashboard_default_period, label = "Form P95 (ms)" }]
        ]
        annotations = {
          horizontal = [
            { value = var.alarm_threshold_p95_ms_form_submit, label = "Form budget (${var.alarm_threshold_p95_ms_form_submit}ms)", color = "#d62728" }
          ]
        }
        yAxis = {
          left = { min = 0 }
        }
      }
    },
    {
      type   = "metric"
      x      = 12
      y      = 0
      width  = 6
      height = 6
      properties = {
        title  = "Audit emit P95 (budget: 100ms)"
        view   = "singleValue"
        region = var.region
        metrics = [
          [local.metric_namespace, "AuditEmitDurationMs", { stat = "p95", period = local.dashboard_default_period, label = "Audit P95 (ms)" }]
        ]
        annotations = {
          horizontal = [
            { value = var.alarm_threshold_audit_emit_ms, label = "Audit budget (${var.alarm_threshold_audit_emit_ms}ms)", color = "#d62728" }
          ]
        }
        yAxis = {
          left = { min = 0 }
        }
      }
    },
    {
      type   = "metric"
      x      = 18
      y      = 0
      width  = 6
      height = 6
      properties = {
        title  = "ALB 5xx target errors (5 min sum)"
        view   = "singleValue"
        region = var.region
        metrics = [
          ["AWS/ApplicationELB", "HTTPCode_Target_5XX_Count", "LoadBalancer", var.alb_arn_suffix, { stat = "Sum", period = local.dashboard_default_period, label = "5xx" }]
        ]
        annotations = {
          horizontal = [
            { value = var.alarm_threshold_5xx_per_5min, label = "Threshold", color = "#d62728" }
          ]
        }
      }
    },
  ]

  #############################################################################
  # Row 2 - ALB / request flow time-series
  #
  # Standard ALB telemetry: request count, target response time (avg + p95),
  # and target health (healthy/unhealthy). Healthy/unhealthy widgets use
  # both the LoadBalancer and TargetGroup dimensions because target-health
  # metrics are scoped per target group.
  #############################################################################
  dashboard_widgets_row2 = [
    {
      type   = "metric"
      x      = 0
      y      = 6
      width  = 6
      height = 6
      properties = {
        title   = "ALB request count"
        view    = "timeSeries"
        stacked = false
        region  = var.region
        metrics = [
          ["AWS/ApplicationELB", "RequestCount", "LoadBalancer", var.alb_arn_suffix, { stat = "Sum", period = local.dashboard_default_period }]
        ]
      }
    },
    {
      type   = "metric"
      x      = 6
      y      = 6
      width  = 6
      height = 6
      properties = {
        title   = "ALB target response time (avg + p95)"
        view    = "timeSeries"
        stacked = false
        region  = var.region
        metrics = [
          ["AWS/ApplicationELB", "TargetResponseTime", "LoadBalancer", var.alb_arn_suffix, { stat = "Average", period = local.dashboard_default_period, label = "avg (s)" }],
          [".", ".", ".", ".", { stat = "p95", period = local.dashboard_default_period, label = "p95 (s)" }]
        ]
      }
    },
    {
      type   = "metric"
      x      = 12
      y      = 6
      width  = 6
      height = 6
      properties = {
        title   = "Healthy target hosts"
        view    = "timeSeries"
        stacked = false
        region  = var.region
        metrics = [
          ["AWS/ApplicationELB", "HealthyHostCount", "TargetGroup", var.alb_target_group_arn_suffix, "LoadBalancer", var.alb_arn_suffix, { stat = "Average", period = local.dashboard_default_period }]
        ]
        yAxis = { left = { min = 0 } }
      }
    },
    {
      type   = "metric"
      x      = 18
      y      = 6
      width  = 6
      height = 6
      properties = {
        title   = "Unhealthy target hosts"
        view    = "timeSeries"
        stacked = false
        region  = var.region
        metrics = [
          ["AWS/ApplicationELB", "UnHealthyHostCount", "TargetGroup", var.alb_target_group_arn_suffix, "LoadBalancer", var.alb_arn_suffix, { stat = "Maximum", period = local.dashboard_default_period }]
        ]
        yAxis = { left = { min = 0 } }
      }
    },
  ]

  #############################################################################
  # Row 3 - ECS / compute time-series
  #
  # CPU + memory utilization come from AWS/ECS (always emitted by ECS).
  # Running task count and deployment count come from ECS/ContainerInsights,
  # which the ECS module enables on the cluster. Without Container Insights
  # those two widgets would be empty; the cluster's Container Insights flag
  # is set in modules/ecs/main.tf so this is safe.
  #
  # Running task count carries a green horizontal annotation at
  # var.ecs_service_desired_count (the desired-state reference). Operators
  # see at a glance whether the running count is at, above, or below
  # desired - critical during deployments and outages.
  #############################################################################
  dashboard_widgets_row3 = [
    {
      type   = "metric"
      x      = 0
      y      = 12
      width  = 6
      height = 6
      properties = {
        title   = "ECS service CPU utilization (%)"
        view    = "timeSeries"
        stacked = false
        region  = var.region
        metrics = [
          ["AWS/ECS", "CPUUtilization", "ClusterName", var.ecs_cluster_name, "ServiceName", var.ecs_service_name, { stat = "Average", period = local.dashboard_default_period }]
        ]
        yAxis = { left = { min = 0, max = 100 } }
      }
    },
    {
      type   = "metric"
      x      = 6
      y      = 12
      width  = 6
      height = 6
      properties = {
        title   = "ECS service memory utilization (%)"
        view    = "timeSeries"
        stacked = false
        region  = var.region
        metrics = [
          ["AWS/ECS", "MemoryUtilization", "ClusterName", var.ecs_cluster_name, "ServiceName", var.ecs_service_name, { stat = "Average", period = local.dashboard_default_period }]
        ]
        yAxis = { left = { min = 0, max = 100 } }
      }
    },
    {
      type   = "metric"
      x      = 12
      y      = 12
      width  = 6
      height = 6
      properties = {
        title   = "ECS running task count"
        view    = "timeSeries"
        stacked = false
        region  = var.region
        metrics = [
          ["ECS/ContainerInsights", "RunningTaskCount", "ClusterName", var.ecs_cluster_name, "ServiceName", var.ecs_service_name, { stat = "Average", period = local.dashboard_default_period }]
        ]
        annotations = {
          horizontal = [
            { value = var.ecs_service_desired_count, label = "Desired count (${var.ecs_service_desired_count})", color = "#2ca02c" }
          ]
        }
        yAxis = { left = { min = 0 } }
      }
    },
    {
      type   = "metric"
      x      = 18
      y      = 12
      width  = 6
      height = 6
      properties = {
        title   = "ECS service deployment count"
        view    = "timeSeries"
        stacked = false
        region  = var.region
        metrics = [
          ["ECS/ContainerInsights", "DeploymentCount", "ClusterName", var.ecs_cluster_name, "ServiceName", var.ecs_service_name, { stat = "Maximum", period = local.dashboard_default_period }]
        ]
        yAxis = { left = { min = 0 } }
      }
    },
  ]

  #############################################################################
  # Row 4 - RDS / data tier time-series
  #
  # Standard RDS health telemetry: CPU, free storage, active connections,
  # read/write latency. CPU is bounded 0-100; the others are unbounded.
  # Connections is informational (no threshold annotation) but the
  # rds_cpu_high alarm in main.tf will fire on sustained high CPU and the
  # rds_storage_free alarm fires on free storage below
  # var.alarm_threshold_rds_free_storage_bytes.
  #############################################################################
  dashboard_widgets_row4 = [
    {
      type   = "metric"
      x      = 0
      y      = 18
      width  = 6
      height = 6
      properties = {
        title   = "RDS CPU utilization (%)"
        view    = "timeSeries"
        stacked = false
        region  = var.region
        metrics = [
          ["AWS/RDS", "CPUUtilization", "DBInstanceIdentifier", var.rds_instance_id, { stat = "Average", period = local.dashboard_default_period }]
        ]
        yAxis = { left = { min = 0, max = 100 } }
      }
    },
    {
      type   = "metric"
      x      = 6
      y      = 18
      width  = 6
      height = 6
      properties = {
        title   = "RDS free storage (bytes)"
        view    = "timeSeries"
        stacked = false
        region  = var.region
        metrics = [
          ["AWS/RDS", "FreeStorageSpace", "DBInstanceIdentifier", var.rds_instance_id, { stat = "Average", period = local.dashboard_default_period }]
        ]
        yAxis = { left = { min = 0 } }
      }
    },
    {
      type   = "metric"
      x      = 12
      y      = 18
      width  = 6
      height = 6
      properties = {
        title   = "RDS DB connections"
        view    = "timeSeries"
        stacked = false
        region  = var.region
        metrics = [
          ["AWS/RDS", "DatabaseConnections", "DBInstanceIdentifier", var.rds_instance_id, { stat = "Average", period = local.dashboard_default_period }]
        ]
        yAxis = { left = { min = 0 } }
      }
    },
    {
      type   = "metric"
      x      = 18
      y      = 18
      width  = 6
      height = 6
      properties = {
        title   = "RDS read/write latency (s)"
        view    = "timeSeries"
        stacked = false
        region  = var.region
        metrics = [
          ["AWS/RDS", "ReadLatency", "DBInstanceIdentifier", var.rds_instance_id, { stat = "Average", period = local.dashboard_default_period, label = "Read latency (s)" }],
          [".", "WriteLatency", ".", ".", { stat = "Average", period = local.dashboard_default_period, label = "Write latency (s)" }]
        ]
        yAxis = { left = { min = 0 } }
      }
    },
  ]

  #############################################################################
  # Row 5 - Custom SLI / business metrics + dashboard text overview
  #
  # The first three widgets re-use the same custom-metric namespace as the
  # row-1 headlines but query SampleCount instead of p95, giving operators
  # the volume of AI calls, form submits, and audit emissions in each
  # 5-minute window. Sample-count over a 5-minute Sum window is the
  # canonical "rate" measurement for log-derived metrics.
  #
  # The fourth widget is a markdown text widget that surfaces the dashboard's
  # purpose, the four performance budgets (per AAP Sec 0.7.3), the alarm
  # SNS topic name, and the count of subscribers. Operators reading the
  # dashboard get quick context without consulting external documentation.
  #
  # Subscriber count is computed as length(var.alarm_email_addresses) plus
  # 1 when var.enable_pagerduty is true (each email is one subscription;
  # PagerDuty is one additional HTTPS subscription).
  #############################################################################
  dashboard_widgets_row5 = [
    {
      type   = "metric"
      x      = 0
      y      = 24
      width  = 6
      height = 6
      properties = {
        title   = "AI call count (5 min sum)"
        view    = "timeSeries"
        stacked = false
        region  = var.region
        metrics = [
          [local.metric_namespace, "AICallDurationMs", { stat = "SampleCount", period = local.dashboard_default_period, label = "AI calls" }]
        ]
        yAxis = { left = { min = 0 } }
      }
    },
    {
      type   = "metric"
      x      = 6
      y      = 24
      width  = 6
      height = 6
      properties = {
        title   = "Form submit count (5 min sum)"
        view    = "timeSeries"
        stacked = false
        region  = var.region
        metrics = [
          [local.metric_namespace, "FormSubmitDurationMs", { stat = "SampleCount", period = local.dashboard_default_period, label = "Form submits" }]
        ]
        yAxis = { left = { min = 0 } }
      }
    },
    {
      type   = "metric"
      x      = 12
      y      = 24
      width  = 6
      height = 6
      properties = {
        title   = "Audit emission count (5 min sum)"
        view    = "timeSeries"
        stacked = false
        region  = var.region
        metrics = [
          [local.metric_namespace, "AuditEmitDurationMs", { stat = "SampleCount", period = local.dashboard_default_period, label = "Audit emits" }]
        ]
        yAxis = { left = { min = 0 } }
      }
    },
    {
      type   = "text"
      x      = 18
      y      = 24
      width  = 6
      height = 6
      properties = {
        markdown = "## Sales-Connections - ${var.environment}\n\n**Performance Budgets** (per AAP Sec 0.7.3)\n\n- AI note gen P95 <= ${var.alarm_threshold_p95_ms_ai_call} ms\n- Form submit P95 <= ${var.alarm_threshold_p95_ms_form_submit} ms\n- Audit emit P95 <= ${var.alarm_threshold_audit_emit_ms} ms\n- ALB 5xx (5 min) < ${var.alarm_threshold_5xx_per_5min}\n\n**Alarm SNS topic**: `${aws_sns_topic.alarms.name}`\n\n**Subscribers**: ${length(var.alarm_email_addresses)} email${var.enable_pagerduty ? " + PagerDuty" : ""}"
      }
    },
  ]

  #############################################################################
  # Combined widget list. var.dashboard_widgets_extra is appended at the
  # bottom so callers can layer in additional widgets (e.g., per-environment
  # custom panels, per-tenant counters) without forking the module. The
  # caller is responsible for supplying widget objects that conform to the
  # CloudWatch Dashboard Body Structure JSON schema.
  #
  # Order matters: rows 1-5 occupy y=0..29 (each row height 6). Extra
  # widgets supplied by the caller should set y >= 30 to avoid collision
  # with the module-defined widgets above.
  #############################################################################
  dashboard_widgets_all = concat(
    local.dashboard_widgets_row1,
    local.dashboard_widgets_row2,
    local.dashboard_widgets_row3,
    local.dashboard_widgets_row4,
    local.dashboard_widgets_row5,
    var.dashboard_widgets_extra,
  )

  #############################################################################
  # Final dashboard body: a single JSON document wrapping the widget list.
  #
  # jsonencode() validates structure at terraform plan time - any HCL-side
  # type mismatch or missing key surfaces as a plan-time error rather than
  # at apply time when the AWS API rejects the body. The resulting JSON
  # is what aws_cloudwatch_dashboard.main.dashboard_body consumes verbatim.
  #############################################################################
  dashboard_body_json = jsonencode({
    widgets = local.dashboard_widgets_all
  })
}

###############################################################################
# Main CloudWatch dashboard resource
#
# The dashboard_name is var.name_prefix (e.g., "sales-connections-prod")
# rather than "${var.name_prefix}-dashboard" so the URL stays short and
# memorable. Operators visit
#   /cloudwatch/home#dashboards:name=sales-connections-prod
# rather than
#   /cloudwatch/home#dashboards:name=sales-connections-prod-dashboard.
#
# Note: aws_cloudwatch_dashboard does not support tags (the resource has
# no tags argument as of AWS provider 5.x). Tagging is achieved indirectly
# via the AWS provider's default_tags configuration in providers.tf.
###############################################################################

resource "aws_cloudwatch_dashboard" "main" {
  dashboard_name = var.name_prefix
  dashboard_body = local.dashboard_body_json
}

