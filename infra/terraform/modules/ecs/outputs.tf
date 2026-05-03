###############################################################################
# infra/terraform/modules/ecs/outputs.tf
#
# Outputs surfaced by the ECS Fargate module. Consumed by:
#   - infra/terraform/main.tf (root composition module wiring)
#   - infra/terraform/outputs.tf (re-exported as root composition outputs)
#   - infra/terraform/modules/database/ (security_group_id for RDS ingress)
#   - infra/terraform/modules/secrets/  (task_role_arn for secret read perms)
#   - infra/terraform/modules/ecr/      (execution_role_arn for image pulls)
#   - infra/terraform/modules/alb/      (security_group_id for ALB egress)
#   - infra/terraform/modules/observability/ (cluster_name, service_name for
#                                              CloudWatch alarms and dashboards)
#
# These outputs are the public CONTRACT of the ecs module. Renaming any
# output here is a breaking change to the entire Terraform composition.
#
# CRITICAL SECURITY INVARIANT (AAP Sec 0.7.4):
#   This module produces no secret values. Outputs surface only ARNs, names,
#   and IDs that point to provisioned AWS resources. ECS task/execution
#   role ARNs ARE NOT secrets - they are reference identifiers visible in
#   CloudTrail and Terraform state.
#
# Convention:
#   - Every output declares description (mandatory per Authoring Conventions).
#   - No sensitive = true markers: ARNs/IDs are not secrets per se.
#   - Frontend-related outputs use try() for null-safety when
#     var.deploy_frontend_container == false.
###############################################################################

###############################################################################
# ECS Cluster outputs
###############################################################################

output "cluster_name" {
  description = "Name of the ECS cluster (e.g., 'sales-connections-prod-cluster'). Consumed by modules/observability/ for CloudWatch metric alarms scoped to this cluster (CPU, memory, task count) and by the operations runbook for `aws ecs update-service` invocations."
  value       = aws_ecs_cluster.main.name
}

output "cluster_arn" {
  description = "Full ARN of the ECS cluster. Used in IAM policy resource scoping and for cross-module reference (e.g., capacity provider associations)."
  value       = aws_ecs_cluster.main.arn
}

output "cluster_id" {
  description = "ECS cluster ID. Surfaced for diagnostic reference; in practice equivalent to cluster_arn for ECS clusters."
  value       = aws_ecs_cluster.main.id
}

###############################################################################
# ECS Service outputs
###############################################################################

output "service_name" {
  description = "Name of the backend ECS service (e.g., 'sales-connections-prod-backend'). Consumed by modules/observability/ for service-level CloudWatch alarms and by .github/workflows/cd.yml for `aws ecs update-service --force-new-deployment` invocations during deploys."
  value       = aws_ecs_service.backend.name
}

output "service_id" {
  description = "Full ARN/ID of the backend ECS service. Surfaced for diagnostic reference and IAM policy resource scoping."
  value       = aws_ecs_service.backend.id
}

output "frontend_service_name" {
  description = "Name of the frontend ECS service (when var.deploy_frontend_container = true); empty string otherwise. Consumed by modules/observability/ when the frontend container deployment mode is active."
  value       = try(aws_ecs_service.frontend[0].name, "")
}

###############################################################################
# Task Definition outputs
###############################################################################

output "task_definition_family" {
  description = "Family name of the backend task definition (e.g., 'sales-connections-prod-backend'). The CD pipeline uses this to register new revisions via `aws ecs register-task-definition` and to bump the service to a new revision. The Terraform service resource has lifecycle.ignore_changes = [task_definition], so revisions are allowed to drift from the Terraform-managed baseline."
  value       = aws_ecs_task_definition.backend.family
}

output "task_definition_arn" {
  description = "ARN of the backend task definition revision currently managed by Terraform. The CD pipeline registers new revisions outside of Terraform; this output reflects only the Terraform baseline."
  value       = aws_ecs_task_definition.backend.arn
}

output "task_definition_revision" {
  description = "Revision number of the Terraform-managed task definition baseline. Useful for diagnostic comparisons against what the CD pipeline has deployed."
  value       = aws_ecs_task_definition.backend.revision
}

output "frontend_task_definition_family" {
  description = "Family name of the frontend task definition (when var.deploy_frontend_container = true); empty string otherwise."
  value       = try(aws_ecs_task_definition.frontend[0].family, "")
}

output "frontend_task_definition_arn" {
  description = "ARN of the frontend task definition revision (when var.deploy_frontend_container = true); empty string otherwise."
  value       = try(aws_ecs_task_definition.frontend[0].arn, "")
}

###############################################################################
# IAM Role outputs (two-role separation per AAP Sec 0.4.6)
#
# Task role: runtime application identity (least-privilege secret reads,
#            log writes, optional X-Ray tracing).
# Execution role: image pull from ECR + container log emission to CloudWatch
#                 + secret resolution at task start.
###############################################################################

output "task_role_arn" {
  description = "ARN of the IAM task role (runtime application identity). Consumed by modules/secrets/ for resource policy reader_principal_arns and by any future modules that need to grant runtime AWS access to the application. Per AAP Sec 0.4.6 two-role separation: task role NEVER has image pull permissions."
  value       = aws_iam_role.task.arn
}

output "task_role_name" {
  description = "Name of the IAM task role. Surfaced for IAM policy authoring and CloudTrail forensics."
  value       = aws_iam_role.task.name
}

output "execution_role_arn" {
  description = "ARN of the IAM execution role (image pull + log emission + secret resolution). Consumed by modules/ecr/ for pull_role_arns. Per AAP Sec 0.4.6 two-role separation: execution role NEVER has direct application-data permissions."
  value       = aws_iam_role.execution.arn
}

output "execution_role_name" {
  description = "Name of the IAM execution role. Surfaced for IAM policy authoring and CloudTrail forensics."
  value       = aws_iam_role.execution.name
}

###############################################################################
# Security Group output
###############################################################################

output "security_group_id" {
  description = "Security group ID attached to ECS tasks. Consumed by modules/database/ for RDS ingress (the ECS task SG is the only allowed source on RDS port 5432) and by modules/alb/ for ALB egress (ALB egress flows to ECS tasks on the backend container port). This SG is the network-layer enforcement of the principle of least connectivity."
  value       = aws_security_group.tasks.id
}

output "security_group_arn" {
  description = "ARN of the ECS task security group. Surfaced for IAM policies that require ARN-level scoping."
  value       = aws_security_group.tasks.arn
}
