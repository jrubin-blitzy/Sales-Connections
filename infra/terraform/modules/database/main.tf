###############################################################################
# infra/terraform/modules/database/main.tf
#
# AWS RDS PostgreSQL 17.7 Multi-AZ database resources for the
# Sales-Connections platform.
#
# Resources provisioned (in dependency order within the module):
#
#   1. aws_iam_role.rds_enhanced_monitoring               (Enhanced Monitoring role)
#   2. aws_iam_role_policy_attachment.rds_enhanced_monitoring_policy
#   3. aws_db_subnet_group.main                           (private-subnets only)
#   4. aws_db_parameter_group.main                        (Postgres 17 params)
#   5. aws_security_group.db                              (port 5432 SG)
#   6. aws_security_group_rule.db_ingress_from_ecs[*]     (ingress from ECS SGs)
#   7. aws_db_instance.main                               (the RDS instance)
#
# Mandatory security invariants (per AAP Sec 0.4.7 / 0.7.4 and folder spec):
#   - storage_encrypted = true is HARDCODED (not a variable).
#   - Master password is sourced from Secrets Manager via
#     data.aws_secretsmanager_secret_version (declared in data.tf).
#   - RDS lives in private subnets only (var.private_subnet_ids).
#   - Ingress on port 5432 is restricted to var.allowed_security_group_ids
#     (typically the ECS task SG); no 0.0.0.0/0 ingress.
#   - lifecycle { ignore_changes = [password, ...] } on aws_db_instance.main
#     allows password rotation by an external Lambda without Terraform drift.
#
# Performance / observability defaults:
#   - Enhanced Monitoring at 60-second intervals.
#   - Performance Insights enabled (7-day free-tier retention).
#   - CloudWatch log exports for "postgresql" and "upgrade" log streams.
#   - Parameter group enables pg_stat_statements and slow-query logging
#     (>= 1 second).
#
# Build order dependencies (per AAP Sec 0.5.2):
#   Inputs from modules/network:  vpc_id, private_subnet_ids
#   Inputs from modules/secrets:  master_password_secret_arn
#   Inputs from modules/ecs:      allowed_security_group_ids (ECS task SG)
#   Outputs consumed by:
#     - modules/ecs (endpoint, port, database_name, master_username)
#     - modules/observability (instance_id for metric alarms)
#     - root infra/terraform/outputs.tf (re-exported for runbooks)
###############################################################################

###############################################################################
# Enhanced Monitoring IAM role
#
# RDS Enhanced Monitoring requires an IAM role that the RDS service can
# assume to publish OS-level metrics (CPU, memory, network, disk I/O) to
# CloudWatch at the configured monitoring_interval. The role is provisioned
# inside this module so it follows the lifecycle of the database instance.
#
# Trust policy: the only permitted principal is the RDS Enhanced Monitoring
# service (monitoring.rds.amazonaws.com). The AWS-managed policy
# AmazonRDSEnhancedMonitoringRole is attached below to grant the necessary
# CloudWatch Logs and Metrics permissions; no inline policy is needed.
###############################################################################

resource "aws_iam_role" "rds_enhanced_monitoring" {
  name        = "${var.name_prefix}-rds-monitoring"
  description = "Allows RDS Enhanced Monitoring to publish OS-level metrics to CloudWatch for the Sales-Connections RDS instance"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect = "Allow"
        Principal = {
          Service = "monitoring.rds.amazonaws.com"
        }
        Action = "sts:AssumeRole"
      },
    ]
  })

  tags = merge(local.module_tags, {
    Name = "${var.name_prefix}-rds-monitoring"
  })
}

resource "aws_iam_role_policy_attachment" "rds_enhanced_monitoring_policy" {
  role = aws_iam_role.rds_enhanced_monitoring.name
  # data.aws_partition.current is declared in data.tf. Using the partition
  # data source makes the managed-policy ARN portable across AWS partitions
  # (standard, GovCloud, China) without modification.
  policy_arn = "arn:${data.aws_partition.current.partition}:iam::aws:policy/service-role/AmazonRDSEnhancedMonitoringRole"
}

###############################################################################
# DB Subnet Group
#
# RDS lives in private subnets ONLY (per the folder spec critical constraint
# "Private subnets only - RDS is unreachable from the public Internet"). The
# Multi-AZ standby is automatically placed in a different AZ within this
# subnet group, providing the synchronous-replication failover target.
#
# The lifecycle.precondition block runs at plan time (before any AWS API
# call) and rejects an input list shorter than 2. variables.tf already
# enforces the same condition via its own validation block; declaring the
# precondition here as well provides defense-in-depth against the variable
# validation being relaxed in the future.
###############################################################################

resource "aws_db_subnet_group" "main" {
  name        = "${var.name_prefix}-db-subnet-group"
  description = "Private subnets hosting the Sales-Connections RDS PostgreSQL instance"
  subnet_ids  = var.private_subnet_ids

  tags = merge(local.module_tags, {
    Name = "${var.name_prefix}-db-subnet-group"
  })

  lifecycle {
    precondition {
      condition     = length(var.private_subnet_ids) >= 2
      error_message = "var.private_subnet_ids must contain at least 2 entries to support Multi-AZ deployment (the synchronous standby requires a subnet in a different AZ from the writer)."
    }
  }
}

###############################################################################
# DB Parameter Group
#
# Per folder spec recommended parameters:
#   - log_min_duration_statement = 1000      (slow query log threshold, ms)
#   - log_connections            = 1         (audit connection events)
#   - log_disconnections         = 1         (audit disconnection events)
#   - log_lock_waits             = 1         (log lock-wait events)
#   - shared_preload_libraries   = pg_stat_statements
#   - pg_stat_statements.track   = all       (track all statement classes)
#
# apply_method = "pending-reboot" is REQUIRED for static parameters like
# shared_preload_libraries, which loads at PostgreSQL startup; specifying
# "immediate" here would silently fail to take effect. The remaining
# (dynamic) parameters use apply_method = "immediate" so they take effect
# on the next query without a reboot.
#
# create_before_destroy is set so the new parameter group can be created
# (and associated with the instance via a separate apply) before the old
# one is destroyed, avoiding "in use" errors during in-place updates that
# replace the parameter group.
###############################################################################

resource "aws_db_parameter_group" "main" {
  name        = "${var.name_prefix}-postgres${local.engine_version_major}"
  family      = local.parameter_group_family
  description = "Sales-Connections PostgreSQL ${var.engine_version} parameter group with slow-query logging, connection auditing, and pg_stat_statements"

  # Slow-query logging: emit a log entry for any statement exceeding 1
  # second. Threshold matches the F-013 audit emission budget so slow
  # queries surface alongside audit events for correlation.
  parameter {
    name         = "log_min_duration_statement"
    value        = "1000"
    apply_method = "immediate"
  }

  # Connection audit: log connection establishment and teardown so an
  # operator can correlate session lifecycles with application logs via
  # the structlog correlation_id header.
  parameter {
    name         = "log_connections"
    value        = "1"
    apply_method = "immediate"
  }

  parameter {
    name         = "log_disconnections"
    value        = "1"
    apply_method = "immediate"
  }

  # Lock-wait diagnostics: emit a log entry for any session that waits
  # longer than deadlock_timeout (default 1s) for a lock. Useful for
  # diagnosing contention on records and audit_events under load.
  parameter {
    name         = "log_lock_waits"
    value        = "1"
    apply_method = "immediate"
  }

  # pg_stat_statements: query-level metrics consumed by Performance
  # Insights and the CloudWatch dashboard. shared_preload_libraries is
  # static (loaded at startup), so apply_method MUST be "pending-reboot".
  parameter {
    name         = "shared_preload_libraries"
    value        = "pg_stat_statements"
    apply_method = "pending-reboot"
  }

  parameter {
    name         = "pg_stat_statements.track"
    value        = "all"
    apply_method = "immediate"
  }

  tags = merge(local.module_tags, {
    Name = "${var.name_prefix}-postgres${local.engine_version_major}"
  })

  lifecycle {
    create_before_destroy = true
  }
}

###############################################################################
# DB Security Group
#
# Permits inbound TCP 5432 (PostgreSQL) only from the security groups
# supplied in var.allowed_security_group_ids (typically the ECS task SG).
# No CIDR-based ingress; no 0.0.0.0/0 rule. Egress is intentionally
# permissive (default) - the database initiates outbound connections to
# CloudWatch Logs, Performance Insights, and Enhanced Monitoring endpoints
# and would silently fail to publish telemetry without it. Restricting
# egress further would require interface VPC endpoints for those services
# and is out of scope for MVP.
#
# name_prefix (vs. name) ensures uniqueness across plans where the SG is
# replaced; the AWS provider generates a random suffix appended to the
# prefix. lifecycle { create_before_destroy = true } is required for SGs
# with name_prefix to avoid name collisions on replacement.
###############################################################################

resource "aws_security_group" "db" {
  name_prefix = "${var.name_prefix}-db-"
  description = "RDS PostgreSQL SG; ingress from ECS tasks SG on port 5432"
  vpc_id      = var.vpc_id

  # Default egress permits the RDS instance to reach AWS service endpoints
  # (CloudWatch Logs, Performance Insights, Enhanced Monitoring). Without
  # this rule, RDS would silently fail to publish logs and metrics.
  egress {
    description = "All egress (RDS reaches CloudWatch Logs, Performance Insights, Enhanced Monitoring endpoints)"
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }

  tags = merge(local.module_tags, {
    Name = "${var.name_prefix}-db-sg"
  })

  lifecycle {
    create_before_destroy = true
  }
}

###############################################################################
# Ingress rules: one per allowed_security_group_ids entry
#
# Using aws_security_group_rule (separate-from-aws_security_group) rather
# than inline ingress blocks because:
#   1. It permits zero ingress rules cleanly when the list is empty
#      (e.g., during initial bootstrap before ECS exists).
#   2. It avoids subtle cycle-detection issues when the source SG is
#      managed by another module (modules/ecs/).
#   3. It allows incrementally adding/removing source SGs via count.
###############################################################################

resource "aws_security_group_rule" "db_ingress_from_ecs" {
  count = length(var.allowed_security_group_ids)

  type                     = "ingress"
  description              = "PostgreSQL ingress from peer security group"
  from_port                = 5432
  to_port                  = 5432
  protocol                 = "tcp"
  security_group_id        = aws_security_group.db.id
  source_security_group_id = var.allowed_security_group_ids[count.index]
}

###############################################################################
# RDS PostgreSQL 17.7 Instance
#
# Multi-AZ when var.multi_az = true (REQUIRED for prod per AAP Sec 0.4.7
# and folder spec critical constraints). Storage is encrypted at rest
# (HARDCODED storage_encrypted = true; not a variable). Master password
# is sourced from Secrets Manager via data.aws_secretsmanager_secret_version
# (declared in data.tf) so the value never appears in Terraform configuration.
#
# CRITICAL: The lifecycle { ignore_changes = [password, ...] } block is
# REQUIRED to allow:
#   - External rotation of the master password by a rotation Lambda (the
#     password value in Secrets Manager changes; Terraform must not
#     attempt to revert RDS to the version it last read).
#   - final_snapshot_identifier rotation (the timestamp-based name changes
#     every plan run; ignoring it prevents drift loops).
###############################################################################

resource "aws_db_instance" "main" {
  identifier = "${var.name_prefix}-postgres"

  # Engine
  engine                     = "postgres"
  engine_version             = var.engine_version
  auto_minor_version_upgrade = true

  # Compute and storage. storage_encrypted = true is HARDCODED (not a
  # variable) per the folder spec critical constraint: encryption-at-rest
  # is a non-negotiable security invariant. local.kms_key_id_or_null
  # coerces an empty-string var.kms_key_id to null so the AWS-managed
  # alias/aws/rds key is used by default.
  instance_class        = var.instance_class
  allocated_storage     = var.allocated_storage
  max_allocated_storage = var.max_allocated_storage
  storage_type          = "gp3"
  storage_encrypted     = true
  kms_key_id            = local.kms_key_id_or_null

  # Database identity and credentials. The master password is sourced
  # from Secrets Manager. Reading it here makes it visible only inside
  # the Terraform plan/state; it never appears in Terraform configuration
  # files. Per the folder spec critical constraint: "NEVER hardcoded;
  # read via data.aws_secretsmanager_secret_version".
  db_name  = var.database_name
  username = var.master_username
  password = data.aws_secretsmanager_secret_version.master_password.secret_string

  # Network placement: private subnets only, restricted ingress, no
  # public IP. Port is set explicitly rather than relying on the engine
  # default to prevent surprises if a future provider version changes
  # the default.
  db_subnet_group_name   = aws_db_subnet_group.main.name
  vpc_security_group_ids = [aws_security_group.db.id]
  publicly_accessible    = false
  port                   = 5432

  # Parameter group
  parameter_group_name = aws_db_parameter_group.main.name

  # High availability
  multi_az = var.multi_az

  # Backups
  backup_retention_period  = var.backup_retention_period
  backup_window            = var.backup_window
  copy_tags_to_snapshot    = true
  delete_automated_backups = true

  # Maintenance
  maintenance_window = var.maintenance_window
  apply_immediately  = var.apply_immediately

  # Deletion protection. final_snapshot_identifier is timestamp-suffixed
  # so destroys with skip_final_snapshot = false succeed without name
  # collisions; the lifecycle block below ignores this field so plan
  # output does not show drift on every run.
  deletion_protection       = var.deletion_protection
  skip_final_snapshot       = var.skip_final_snapshot
  final_snapshot_identifier = var.skip_final_snapshot ? null : "${var.name_prefix}-postgres-final-${formatdate("YYYY-MM-DD-hh-mm", timestamp())}"

  # Performance Insights. Free-tier retention (7 days) is selected when
  # PI is enabled; null when PI is disabled (the AWS provider rejects 0).
  performance_insights_enabled          = var.performance_insights_enabled
  performance_insights_retention_period = var.performance_insights_enabled ? 7 : null

  # Enhanced Monitoring at 60-second intervals (the highest-frequency
  # tier that does not materially impact instance performance). The
  # role ARN points to the IAM role provisioned above.
  monitoring_interval = 60
  monitoring_role_arn = aws_iam_role.rds_enhanced_monitoring.arn

  # CloudWatch log exports: PostgreSQL query log and engine upgrade log
  # streams are forwarded to CloudWatch Logs for centralized retention
  # and dashboarding.
  enabled_cloudwatch_logs_exports = ["postgresql", "upgrade"]

  tags = merge(local.module_tags, {
    Name = "${var.name_prefix}-postgres"
  })

  lifecycle {
    # Ignore changes to fields that are managed externally:
    #   - password: rotated by an external Lambda or operator. Without
    #     ignore_changes, every apply after rotation would attempt to
    #     revert RDS to the password value last read by Terraform.
    #   - final_snapshot_identifier: timestamp-based; would otherwise
    #     show drift on every plan.
    ignore_changes = [
      password,
      final_snapshot_identifier,
    ]

    precondition {
      condition     = length(var.private_subnet_ids) >= 2
      error_message = "var.private_subnet_ids must contain at least 2 entries; Multi-AZ deployment requires a standby subnet in a different AZ."
    }

    precondition {
      condition     = var.max_allocated_storage >= var.allocated_storage
      error_message = "var.max_allocated_storage (${var.max_allocated_storage}) must be greater than or equal to var.allocated_storage (${var.allocated_storage}); RDS rejects storage auto-scaling configurations otherwise."
    }
  }

  # Explicit dependency ordering: the Enhanced Monitoring role policy
  # attachment must be in place before RDS attempts to assume the role
  # at instance creation. Terraform infers other dependencies (subnet
  # group, parameter group, security group) via attribute references.
  depends_on = [
    aws_iam_role_policy_attachment.rds_enhanced_monitoring_policy,
  ]
}
