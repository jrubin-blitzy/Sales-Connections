###############################################################################
# infra/terraform/modules/ecr/data.tf
#
# Data sources for the ECR module. Currently houses the aws_iam_policy_document
# used by aws_ecr_repository_policy resources in main.tf.
#
# Why a single shared policy document:
#   The push/pull access pattern is identical for backend and frontend repos
#   (same deployer role pushes both; same ECS execution role pulls both).
#   Authoring one document and reusing it across two policy resources keeps
#   the access matrix in lockstep and avoids drift between repositories.
#
# Authoritative principals (per AAP Sec 0.4.6 two-role separation):
#   - Push: var.push_role_arn (GitHub Actions OIDC deploy role)
#   - Pull: var.pull_role_arns (typically [ECS task execution role])
#
# Conditional creation:
#   The data source is count-gated by local.create_repository_policy so that
#   when neither push_role_arn nor pull_role_arns are configured, the policy
#   document is NOT generated, mirroring the count guard on the corresponding
#   aws_ecr_repository_policy resources.
#
# Consumers (in main.tf):
#   - aws_ecr_repository_policy.backend.policy =
#       data.aws_iam_policy_document.repository[0].json
#   - aws_ecr_repository_policy.frontend.policy =
#       data.aws_iam_policy_document.repository[0].json
#
# Cross-references:
#   - Pull actions per folder spec: ecr:GetDownloadUrlForLayer,
#     ecr:BatchGetImage, ecr:BatchCheckLayerAvailability.
#   - Push actions per folder spec: ecr:PutImage, ecr:InitiateLayerUpload,
#     ecr:UploadLayerPart, ecr:CompleteLayerUpload,
#     ecr:BatchCheckLayerAvailability.
#   - ecr:BatchCheckLayerAvailability appears in BOTH pull and push by
#     design: pull consumers use it to verify layer availability before
#     fetching; the deployer uses it during push to detect existing
#     layers and skip re-upload (saves bandwidth).
###############################################################################

###############################################################################
# Repository policy document (shared by backend and frontend repository
# policies in main.tf). Count-gated by local.create_repository_policy so
# this data source is only evaluated when at least one principal is
# configured.
#
# Each statement is wrapped in a dynamically-included block so the document
# omits empty statements when its corresponding principal variable is
# unset. Without that conditional inclusion, a statement with an empty
# identifiers list would emit an invalid IAM policy that AWS rejects at
# apply time.
###############################################################################

data "aws_iam_policy_document" "repository" {
  count = local.create_repository_policy ? 1 : 0

  #############################################################################
  # Statement 1: Pull access for consumer roles (typically the ECS task
  # execution role). The execution role is what AWS itself uses to pull
  # the image when launching a Fargate task; the application's runtime
  # task role is NOT involved in image pulls.
  #
  # Actions per folder spec critical requirements:
  #   - ecr:GetDownloadUrlForLayer
  #   - ecr:BatchGetImage
  #   - ecr:BatchCheckLayerAvailability
  #
  # The for_each guard ensures the statement is only emitted when
  # var.pull_role_arns is non-empty, allowing the module to be applied
  # during initial bootstrap (before consumer roles exist) without the
  # AWS API rejecting an empty principals block.
  #############################################################################
  dynamic "statement" {
    for_each = length(var.pull_role_arns) > 0 ? [1] : []
    content {
      sid    = "AllowPullByConsumers"
      effect = "Allow"

      principals {
        type        = "AWS"
        identifiers = var.pull_role_arns
      }

      actions = [
        "ecr:GetDownloadUrlForLayer",
        "ecr:BatchGetImage",
        "ecr:BatchCheckLayerAvailability",
      ]
    }
  }

  #############################################################################
  # Statement 2: Push access for the deployer role (typically the GitHub
  # Actions OIDC-federated deploy role assumed by .github/workflows/cd.yml).
  #
  # Actions per folder spec critical requirements:
  #   - ecr:PutImage
  #   - ecr:InitiateLayerUpload
  #   - ecr:UploadLayerPart
  #   - ecr:CompleteLayerUpload
  #   - ecr:BatchCheckLayerAvailability (also needed during push to detect
  #     existing layers and skip re-upload)
  #
  # The for_each guard ensures the statement is only emitted when
  # var.push_role_arn is set, allowing pull-only configurations (e.g., a
  # consumer-only environment that pulls images built and pushed from
  # a different account).
  #############################################################################
  dynamic "statement" {
    for_each = var.push_role_arn != "" ? [1] : []
    content {
      sid    = "AllowPushByDeployer"
      effect = "Allow"

      principals {
        type        = "AWS"
        identifiers = [var.push_role_arn]
      }

      actions = [
        "ecr:PutImage",
        "ecr:InitiateLayerUpload",
        "ecr:UploadLayerPart",
        "ecr:CompleteLayerUpload",
        "ecr:BatchCheckLayerAvailability",
      ]
    }
  }
}
