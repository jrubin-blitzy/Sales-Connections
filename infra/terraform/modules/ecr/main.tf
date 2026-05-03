###############################################################################
# infra/terraform/modules/ecr/main.tf
#
# ECR private container image repositories for the Sales-Connections platform.
#
# Resources provisioned:
#   1. aws_ecr_repository.backend                  (always created)
#   2. aws_ecr_lifecycle_policy.backend            (always created)
#   3. aws_ecr_repository_policy.backend           (count = local.create_repository_policy ? 1 : 0)
#   4. aws_ecr_repository.frontend                 (count = var.create_frontend_repo ? 1 : 0)
#   5. aws_ecr_lifecycle_policy.frontend           (count = var.create_frontend_repo ? 1 : 0)
#   6. aws_ecr_repository_policy.frontend          (count = var.create_frontend_repo && local.create_repository_policy ? 1 : 0)
#
# Critical constraints (per folder spec / AAP):
#   - scan_on_push = true is MANDATORY (native ECR scanner per AAP Sec 0.4.6).
#   - image_tag_mutability = "IMMUTABLE" for prod; "MUTABLE" for dev.
#   - Encryption at rest: AES256 default; KMS when var.kms_key_id is non-empty.
#   - Repository policy authored via aws_iam_policy_document (no inline JSON).
#   - Lifecycle policy expires untagged after var.lifecycle_policy_untagged_days
#     and caps tagged at var.lifecycle_policy_keep_count.
#   - Every resource merges local.module_tags (which includes Component = "ECR").
#
# Resource graph (within this module):
#
#   data.aws_iam_policy_document.repository (defined in data.tf)
#     |-> aws_ecr_repository_policy.backend  (consumes data.json)
#     |-> aws_ecr_repository_policy.frontend (consumes data.json)
#
#   aws_ecr_repository.backend
#     |-> aws_ecr_lifecycle_policy.backend
#     |-> aws_ecr_repository_policy.backend
#
#   aws_ecr_repository.frontend (count-gated)
#     |-> aws_ecr_lifecycle_policy.frontend (count-gated)
#     |-> aws_ecr_repository_policy.frontend (count-gated)
###############################################################################

###############################################################################
# Backend repository (always created)
#
# Every environment provisions a backend image repository. The frontend
# repository is the only conditional one because the SPA can alternatively
# be delivered as static assets via S3+CloudFront.
###############################################################################

resource "aws_ecr_repository" "backend" {
  name                 = local.backend_repo_name
  image_tag_mutability = var.image_tag_mutability
  force_delete         = var.force_delete

  image_scanning_configuration {
    scan_on_push = var.scan_on_push
  }

  encryption_configuration {
    encryption_type = var.encryption_type
    # kms_key is only set when encryption_type = "KMS" AND var.kms_key_id != "".
    # AES256 mode does not accept a kms_key argument; passing null causes the
    # provider to omit the field cleanly from the AWS API call.
    kms_key = var.encryption_type == "KMS" && var.kms_key_id != "" ? var.kms_key_id : null
  }

  tags = merge(local.module_tags, {
    Name = local.backend_repo_name
    Tier = "Backend"
  })
}

###############################################################################
# Backend lifecycle policy
#
# Expires untagged images after var.lifecycle_policy_untagged_days days.
# Caps tagged images at var.lifecycle_policy_keep_count newest entries.
# JSON is composed in locals.tf via local.lifecycle_policy_json.
###############################################################################

resource "aws_ecr_lifecycle_policy" "backend" {
  repository = aws_ecr_repository.backend.name
  policy     = local.lifecycle_policy_json
}

###############################################################################
# Backend repository policy (conditional)
#
# Created when at least one of these is set:
#   - var.push_role_arn != "" (a deployer role that needs to push)
#   - length(var.pull_role_arns) > 0 (consumer roles that need to pull)
#
# When neither is set, the repository is accessible only to the AWS account's
# default IAM principals (i.e., the standard implicit access). This is the
# safe default for greenfield environments where the consumer roles haven't
# been provisioned yet (the AAP Sec 0.5.2 build order applies ECR before ECS,
# so the ECS execution role does not yet exist on the first apply).
###############################################################################

resource "aws_ecr_repository_policy" "backend" {
  count = local.create_repository_policy ? 1 : 0

  repository = aws_ecr_repository.backend.name
  policy     = data.aws_iam_policy_document.repository[0].json
}

###############################################################################
# Frontend repository (count = var.create_frontend_repo ? 1 : 0)
#
# Activated when frontend_delivery_mode == "container" in the root composition,
# meaning the frontend SPA is delivered as an nginx container in ECS+ALB
# rather than as static assets in S3+CloudFront.
###############################################################################

resource "aws_ecr_repository" "frontend" {
  count = var.create_frontend_repo ? 1 : 0

  name                 = local.frontend_repo_name
  image_tag_mutability = var.image_tag_mutability
  force_delete         = var.force_delete

  image_scanning_configuration {
    scan_on_push = var.scan_on_push
  }

  encryption_configuration {
    encryption_type = var.encryption_type
    kms_key         = var.encryption_type == "KMS" && var.kms_key_id != "" ? var.kms_key_id : null
  }

  tags = merge(local.module_tags, {
    Name = local.frontend_repo_name
    Tier = "Frontend"
  })
}

###############################################################################
# Frontend lifecycle policy (conditional)
#
# Same lifecycle policy JSON as the backend (centralized in locals.tf) so
# both repositories share an identical retention posture and avoid drift.
###############################################################################

resource "aws_ecr_lifecycle_policy" "frontend" {
  count = var.create_frontend_repo ? 1 : 0

  repository = aws_ecr_repository.frontend[0].name
  policy     = local.lifecycle_policy_json
}

###############################################################################
# Frontend repository policy (conditional)
#
# Doubly guarded:
#   - var.create_frontend_repo must be true (frontend repo exists)
#   - local.create_repository_policy must be true (push or pull principals set)
#
# When either guard is false, no aws_ecr_repository_policy is created for
# the frontend repository. The shared aws_iam_policy_document from data.tf
# (count-gated by local.create_repository_policy) is reused unchanged.
###############################################################################

resource "aws_ecr_repository_policy" "frontend" {
  count = var.create_frontend_repo && local.create_repository_policy ? 1 : 0

  repository = aws_ecr_repository.frontend[0].name
  policy     = data.aws_iam_policy_document.repository[0].json
}
