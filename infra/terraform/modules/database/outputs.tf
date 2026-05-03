###############################################################################
# infra/terraform/modules/database/outputs.tf
#
# Outputs surfaced by the database module. Consumed by:
#   - infra/terraform/main.tf (root composition module wiring)
#   - infra/terraform/outputs.tf (re-exported as root composition outputs)
#   - infra/terraform/modules/ecs/ (env-var injection for the ECS task
#                                    definition: DB_HOST, DB_PORT, DB_NAME,
#                                    DB_USERNAME)
#   - infra/terraform/modules/observability/ (RDS metric alarms scoped by
#                                              instance_id)
#
# These outputs are the public CONTRACT of the database module. Renaming any
# output here is a breaking change to the entire Terraform composition.
#
# CRITICAL SECURITY INVARIANT (AAP Sec 0.7.4):
#   This file MUST NOT expose the master password. The password lives
#   exclusively in Secrets Manager (managed by modules/secrets/) and is
#   referenced by ARN (modules/secrets/outputs.db_password_arn). This module
#   exposes only host/port/database/username - non-secret components needed
#   for DSN composition.
#
# Convention:
#   - Every output declares description (mandatory per Authoring Conventions).
#   - No `sensitive = true` markers on hostnames/IDs: these are reference
#     identifiers visible in CloudTrail and Terraform state and are not
#     secrets per se.
###############################################################################

###############################################################################
# Connection endpoint outputs
#
# These four values feed the ECS task definition's environment block:
#   DB_HOST=<address>
#   DB_PORT=<port>
#   DB_NAME=<database_name>
#   DB_USERNAME=<master_username>
#
# DB_PASSWORD is sourced separately from Secrets Manager via the ECS
# task definition's `secrets` block referencing module.secrets.db_password_arn.
###############################################################################

output "endpoint" {
  description = "RDS writer endpoint in 'host:port' form (e.g., 'sales-connections-prod-postgres.abcdef.us-east-1.rds.amazonaws.com:5432'). Consumed by the operations runbook and by SQLAlchemy DSN composition. Use 'address' for the hostname-only form."
  value       = aws_db_instance.main.endpoint
}

output "address" {
  description = "RDS writer hostname WITHOUT the port suffix (e.g., 'sales-connections-prod-postgres.abcdef.us-east-1.rds.amazonaws.com'). Consumed by modules/ecs/ for the DB_HOST env var injected into the backend task definition."
  value       = aws_db_instance.main.address
}

output "port" {
  description = "RDS listening port (always 5432 for standard PostgreSQL configuration). Consumed by modules/ecs/ for the DB_PORT env var."
  value       = aws_db_instance.main.port
}

output "database_name" {
  description = "Name of the application database created at provision time (e.g., 'sales_connections'). Consumed by modules/ecs/ for the DB_NAME env var; also by docs/operations.md for runbook references."
  value       = aws_db_instance.main.db_name
}

output "master_username" {
  description = "Master username for the RDS instance (e.g., 'sales_connections_app'). Consumed by modules/ecs/ for the DB_USERNAME env var. The password is NOT surfaced; it lives in Secrets Manager and is referenced by ARN via module.secrets.db_password_arn."
  value       = aws_db_instance.main.username
}

###############################################################################
# Instance identity outputs
#
# Identifiers used by sibling modules (observability, future read-replica
# modules) and by IAM policies that need to scope permissions to this
# specific RDS instance.
###############################################################################

output "instance_id" {
  description = "RDS instance identifier (e.g., 'sales-connections-prod-postgres'). Consumed by modules/observability/ for CloudWatch metric alarms scoped to this instance (CPU, FreeStorageSpace, DatabaseConnections, ReplicaLag) and by the CloudWatch dashboard widgets."
  value       = aws_db_instance.main.identifier
}

output "instance_arn" {
  description = "Full ARN of the RDS instance (e.g., 'arn:aws:rds:us-east-1:123456789012:db:sales-connections-prod-postgres'). Used in IAM policies that scope permissions to this specific RDS instance (e.g., for IAM authentication or for the Performance Insights API)."
  value       = aws_db_instance.main.arn
}

output "instance_resource_id" {
  description = "RDS instance resource identifier (immutable; format 'db-XXXXXXXX'). Used by Performance Insights and by IAM database authentication if enabled in the future. Surfaced for diagnostic reference."
  value       = aws_db_instance.main.resource_id
}

output "instance_availability_zone" {
  description = "Availability zone of the primary RDS writer instance. The Multi-AZ standby (when var.multi_az is true) lives in a different AZ; this output reports the writer only."
  value       = aws_db_instance.main.availability_zone
}

###############################################################################
# Network outputs
#
# Surface the RDS security group ID and DB subnet group identifiers for
# cross-module reference and IAM policy scoping. The security group ID is
# consumed by sibling modules (e.g., observability) that may need to author
# additional ingress rules; the primary ingress (from ECS task SG) is
# already configured inside this module via aws_security_group_rule.db_ingress_from_ecs.
###############################################################################

output "security_group_id" {
  description = "Security group ID attached to the RDS instance. Consumed by modules/observability/ for diagnostic ingress (when needed) and surfaced for cross-module reference. Note: ingress from ECS task SG is configured via aws_security_group_rule.db_ingress_from_ecs in this module's main.tf using var.allowed_security_group_ids."
  value       = aws_security_group.db.id
}

output "security_group_arn" {
  description = "ARN of the RDS security group. Surfaced for IAM policies that require ARN-level scoping."
  value       = aws_security_group.db.arn
}

output "db_subnet_group_name" {
  description = "Name of the DB subnet group containing the private subnets that host the RDS instance and its Multi-AZ standby. Consumed by docs/operations.md and by future modules that may provision read replicas in the same subnet group."
  value       = aws_db_subnet_group.main.name
}

output "db_subnet_group_arn" {
  description = "ARN of the DB subnet group. Surfaced for IAM policies and for cross-module reference."
  value       = aws_db_subnet_group.main.arn
}

###############################################################################
# Parameter group outputs
#
# Surface the parameter group identifiers for operational diagnostics and
# IAM policy scoping. Operators inspect this parameter group when
# investigating slow-query logs (log_min_duration_statement >= 1000 ms),
# connection-audit events (log_connections / log_disconnections), lock-wait
# diagnostics (log_lock_waits), and pg_stat_statements query metrics.
###############################################################################

output "parameter_group_name" {
  description = "Name of the DB parameter group attached to the RDS instance (e.g., 'sales-connections-prod-postgres17'). Surfaced for operational diagnostics: operators inspect this parameter group when investigating slow-query logs or connection-audit events. The parameter group enables log_min_duration_statement, log_connections, log_disconnections, log_lock_waits, and pg_stat_statements per the folder spec."
  value       = aws_db_parameter_group.main.name
}

output "parameter_group_arn" {
  description = "ARN of the DB parameter group. Surfaced for IAM policies and cross-module reference."
  value       = aws_db_parameter_group.main.arn
}

###############################################################################
# KMS / encryption outputs
#
# Per folder spec critical constraints: storage_encrypted = true is
# hardcoded (mandatory). The KMS key is either:
#   - a customer-managed CMK (when var.kms_key_id is non-empty), OR
#   - the AWS-managed key alias/aws/rds (when var.kms_key_id is "")
#
# This output surfaces the actual key ARN used (read from the resource
# attribute, not the input variable, so the AWS-managed key ARN is
# correctly reported when var.kms_key_id is empty).
###############################################################################

output "kms_key_arn" {
  description = "ARN of the KMS key used for RDS storage encryption at rest. When var.kms_key_id is non-empty, this is the customer-managed CMK ARN; otherwise this is the AWS-managed key (alias/aws/rds) ARN. Always populated since storage_encrypted is hardcoded true per the folder spec mandatory security constraints."
  value       = aws_db_instance.main.kms_key_id
}

output "storage_encrypted" {
  description = "Whether RDS storage is encrypted at rest. Always true per the folder spec mandatory security constraints. Surfaced for compliance reporting and audit attestation."
  value       = aws_db_instance.main.storage_encrypted
}

###############################################################################
# Multi-AZ / HA outputs
#
# Reflect the actual Multi-AZ state and configured backup retention.
# Auditors and operators can run 'terraform output -json' to verify
# production conformance to the AAP Sec 0.4.7 Multi-AZ requirement and
# the backup retention budget.
###############################################################################

output "multi_az" {
  description = "Whether the RDS instance is provisioned in Multi-AZ mode (synchronous standby in a second AZ). Reflects var.multi_az; reported here for compliance attestation. Per AAP Sec 0.4.7 and folder spec critical constraints, prod MUST set var.multi_az = true."
  value       = aws_db_instance.main.multi_az
}

output "backup_retention_period" {
  description = "Configured automated backup retention period in days. Surfaced for compliance attestation and runbook references."
  value       = aws_db_instance.main.backup_retention_period
}

###############################################################################
# Enhanced Monitoring IAM role output
#
# The IAM role assumed by the RDS Enhanced Monitoring service to publish
# OS-level metrics (CPU, memory, network, disk I/O) to CloudWatch at the
# configured monitoring_interval. Surfaced for cross-module reference and
# operator diagnostics if Enhanced Monitoring fails.
###############################################################################

output "enhanced_monitoring_role_arn" {
  description = "ARN of the IAM role used by RDS Enhanced Monitoring to publish metrics to CloudWatch. Surfaced for cross-module reference and for operator diagnostics if Enhanced Monitoring fails."
  value       = aws_iam_role.rds_enhanced_monitoring.arn
}
