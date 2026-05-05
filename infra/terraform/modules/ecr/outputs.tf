###############################################################################
# infra/terraform/modules/ecr/outputs.tf
#
# Outputs surfaced by the ECR module. Consumed by:
#   - infra/terraform/main.tf (root composition module wiring)
#   - infra/terraform/outputs.tf (re-exported as root composition outputs)
#   - infra/terraform/modules/ecs/ (image URI construction in task definitions)
#   - .github/workflows/cd.yml (docker push targets)
#
# These outputs are the public CONTRACT of the ecr module. Renaming any
# output here is a breaking change to the entire Terraform composition.
#
# Convention:
#   - Backend outputs reference unconditional resources (no count guard).
#   - Frontend outputs use try(...) so they return "" when
#     var.create_frontend_repo == false (count = 0). This null-safe pattern
#     is mandated by the folder spec.
#   - Every output declares description (mandatory per Authoring Conventions).
#   - No sensitive = true markers: ECR repository URLs, ARNs, names, and
#     registry IDs are not secrets. They appear in CloudTrail logs, ECS task
#     definitions, and CI logs; treating them as sensitive would be misleading.
###############################################################################

###############################################################################
# Backend repository outputs (always populated)
#
# The backend repository is unconditional in main.tf (no count guard), so
# attributes are referenced directly. Wrapping these in try() would mask
# legitimate provisioning failures and is therefore avoided.
###############################################################################

output "backend_repository_url" {
  description = "URL of the backend ECR repository in the form '<account>.dkr.ecr.<region>.amazonaws.com/<name>'. Used by docker push (CD pipeline) and by ECS task definition image references."
  value       = aws_ecr_repository.backend.repository_url
}

output "backend_repository_arn" {
  description = "Full ARN of the backend ECR repository. Used in IAM policy resource scoping (e.g., ECS task execution role's ecr:GetDownloadUrlForLayer permission)."
  value       = aws_ecr_repository.backend.arn
}

output "backend_repository_name" {
  description = "Name (without account/region prefix) of the backend ECR repository, e.g., 'sales-connections-prod-backend'."
  value       = aws_ecr_repository.backend.name
}

output "backend_registry_id" {
  description = "AWS account ID hosting the backend ECR registry. Surfaced for ECR DescribeImages API calls and cross-account image references."
  value       = aws_ecr_repository.backend.registry_id
}

###############################################################################
# Frontend repository outputs (conditional; empty string when
# var.create_frontend_repo == false)
#
# Pattern: try(aws_ecr_repository.frontend[0].X, "") returns the value when
# the resource exists (count = 1) and "" when it doesn't (count = 0).
# This is the null-safe convention mandated by the folder spec.
#
# Empty string defaults (rather than null) let consumers do consistent
# length(...) > 0 checks. The root composition's module.ecs constructs
# image URIs as "${module.ecr.frontend_repository_url}:${tag}"; when
# frontend_repository_url == "" that becomes ":tag" (still a string),
# which the ECS module then guards with its own conditionals.
###############################################################################

output "frontend_repository_url" {
  description = "URL of the frontend ECR repository, or empty string when var.create_frontend_repo is false. Consumed by ECS task definition (frontend container image URI) and the CD pipeline (frontend docker push target)."
  value       = try(aws_ecr_repository.frontend[0].repository_url, "")
}

output "frontend_repository_arn" {
  description = "Full ARN of the frontend ECR repository, or empty string when var.create_frontend_repo is false. Used in IAM policy resource scoping for ECS task execution role pull permissions."
  value       = try(aws_ecr_repository.frontend[0].arn, "")
}

output "frontend_repository_name" {
  description = "Name of the frontend ECR repository, or empty string when var.create_frontend_repo is false."
  value       = try(aws_ecr_repository.frontend[0].name, "")
}

output "frontend_registry_id" {
  description = "AWS account ID hosting the frontend ECR registry, or empty string when var.create_frontend_repo is false."
  value       = try(aws_ecr_repository.frontend[0].registry_id, "")
}
