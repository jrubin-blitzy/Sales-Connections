###############################################################################
# infra/terraform/modules/ecr/locals.tf
#
# Module-internal locals derived from input variables. Centralized here so
# main.tf and data.tf consume canonical expressions rather than duplicating
# the conditional logic.
#
# Locals exposed:
#   - backend_repo_name           : "${var.name_prefix}-backend"
#   - frontend_repo_name          : "${var.name_prefix}-frontend"
#   - create_repository_policy    : true when push_role_arn or pull_role_arns
#                                   is non-empty (gate for repository policy
#                                   resources and the policy data source)
#   - module_tags                 : merge(var.tags, { Component = "ECR",
#                                                     Environment = ... })
#   - lifecycle_policy_json       : JSON-encoded ECR lifecycle policy
#                                   shared by backend and frontend
#                                   aws_ecr_lifecycle_policy resources
#
# Locals here are pure functions of input variables (no resource references,
# no data sources). This means terraform plan evaluates them at parse time
# without an AWS API call, which gives fast feedback for input validation.
###############################################################################

locals {
  #############################################################################
  # Repository name composition
  #
  # Per folder spec: "name = ${var.name_prefix}-backend" and
  # "name = ${var.name_prefix}-frontend". Centralizing here means a future
  # change (e.g., introducing a service prefix) is a one-line edit.
  #
  # Used by:
  #   - aws_ecr_repository.backend            (name = local.backend_repo_name)
  #   - aws_ecr_repository.frontend           (name = local.frontend_repo_name)
  #   - aws_ecr_lifecycle_policy.backend      (repository = local.backend_repo_name)
  #   - aws_ecr_lifecycle_policy.frontend     (repository = local.frontend_repo_name)
  #   - aws_ecr_repository_policy.backend     (repository = local.backend_repo_name)
  #   - aws_ecr_repository_policy.frontend    (repository = local.frontend_repo_name)
  #   - per-resource Name tags                (Name = local.X_repo_name)
  #############################################################################
  backend_repo_name  = "${var.name_prefix}-backend"
  frontend_repo_name = "${var.name_prefix}-frontend"

  #############################################################################
  # Repository policy creation gate
  #
  # Per folder spec: "aws_ecr_repository_policy.backend (CONDITIONAL when
  # var.push_role_arn != \"\" or length(var.pull_role_arns) > 0)".
  #
  # When neither principal is configured, no repository policy is created
  # (relying on standard AWS account-level IAM access). This allows ECR to
  # be applied during initial bootstrap before consumer roles exist.
  #
  # Used by:
  #   - data.aws_iam_policy_document.repository (count guard)
  #   - aws_ecr_repository_policy.backend       (count guard)
  #   - aws_ecr_repository_policy.frontend      (count guard, also gated by
  #                                              var.create_frontend_repo)
  #############################################################################
  create_repository_policy = var.push_role_arn != "" || length(var.pull_role_arns) > 0

  #############################################################################
  # Tag merge: caller-provided var.tags + module-specific labels
  #
  # Per folder spec: "Every resource merges var.tags and adds Component = 'ECR'".
  # Environment is also layered in here (consistent with sibling modules) so
  # cost-explorer breakdowns can isolate ECR-related spend per environment
  # even when the module is used standalone in tests (without parent
  # default_tags).
  #
  # Used by every resource in main.tf via merge(local.module_tags, ...).
  #############################################################################
  module_tags = merge(
    var.tags,
    {
      Component   = "ECR"
      Environment = var.environment
    },
  )

  #############################################################################
  # ECR lifecycle policy JSON
  #
  # Two rules per folder spec:
  #   Rule 1 (priority 1): Expire untagged images after
  #                        var.lifecycle_policy_untagged_days days.
  #   Rule 2 (priority 2): Keep only the most recent
  #                        var.lifecycle_policy_keep_count tagged images.
  #
  # Authored via jsonencode() rather than inline JSON to avoid escaping
  # complexity and to leverage HCL's type system for parameter substitution.
  #
  # tagStatus filtering:
  #   - Rule 1 uses tagStatus = "untagged" (no tagPatternList allowed).
  #   - Rule 2 uses tagStatus = "any" with countType = "imageCountMoreThan"
  #     so it caps the total tagged image count regardless of tag pattern.
  #     ECR evaluates rules in priority order; rule 1 strips untagged first,
  #     then rule 2 caps the remaining (tagged) set.
  #
  # countType notes:
  #   - "sinceImagePushed" requires countUnit = "days" (duration-based).
  #   - "imageCountMoreThan" does NOT accept countUnit (it is a count, not
  #     a duration).
  #
  # action = { type = "expire" } is the only valid action for ECR lifecycle
  # rules.
  #
  # Reference: AWS docs - ECR Lifecycle Policy Examples
  #############################################################################
  lifecycle_policy_json = jsonencode({
    rules = [
      {
        rulePriority = 1
        description  = "Expire untagged images after ${var.lifecycle_policy_untagged_days} day(s)"
        selection = {
          tagStatus   = "untagged"
          countType   = "sinceImagePushed"
          countUnit   = "days"
          countNumber = var.lifecycle_policy_untagged_days
        }
        action = {
          type = "expire"
        }
      },
      {
        rulePriority = 2
        description  = "Keep only the ${var.lifecycle_policy_keep_count} most recent tagged image(s)"
        selection = {
          tagStatus   = "any"
          countType   = "imageCountMoreThan"
          countNumber = var.lifecycle_policy_keep_count
        }
        action = {
          type = "expire"
        }
      },
    ]
  })
}
