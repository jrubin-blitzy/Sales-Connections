###############################################################################
# infra/terraform/modules/secrets/data.tf
#
# IAM policy document data sources used by aws_secretsmanager_secret_policy
# resources in main.tf. Authored via aws_iam_policy_document (not inline JSON)
# per the folder spec authoring conventions:
#
#   "Use aws_iam_policy_document data source for resource policies
#    (no inline JSON)"
#
# Two policy documents, attached to four secret policies:
#
#   1. standard_secret_access  -> attached to:
#        aws_secretsmanager_secret_policy.anthropic_api_key
#        aws_secretsmanager_secret_policy.google_oauth_client_secret
#        aws_secretsmanager_secret_policy.jwt_signing_key
#
#      Grants secretsmanager:GetSecretValue to var.reader_principal_arns
#      (typically the ECS task role).
#
#   2. db_password_access      -> attached to:
#        aws_secretsmanager_secret_policy.db_password
#
#      Grants secretsmanager:GetSecretValue to var.reader_principal_arns,
#      AND (when var.enable_db_password_rotation == true) grants the
#      rotation Lambda the rotation-cycle actions:
#        secretsmanager:GetSecretValue
#        secretsmanager:PutSecretValue
#        secretsmanager:UpdateSecretVersionStage
#        secretsmanager:DescribeSecret
#
# Why two documents (vs one shared document with conditional Lambda
# principal):
#   Splitting prevents rotation-Lambda permissions from leaking onto
#   secrets that should NOT be rotated automatically (anthropic, google,
#   jwt). Failure mode: a rotation bug or compromised Lambda would only
#   affect db_password, not all four secrets. Defense-in-depth.
#
# CRITICAL SECURITY INVARIANTS (per AAP Sec 0.7.4):
#   - No wildcard (*) principals: every Allow statement references explicit
#     IAM role ARNs sourced from variables.
#   - No condition-less wildcards: actions are restricted to the minimum
#     necessary for each consumer.
#   - No Deny statements: AWS resource policies default-deny what isn't
#     explicitly allowed; adding Deny would be defensive duplication.
###############################################################################

###############################################################################
# Standard secret access policy document
#
# Attached to: anthropic_api_key, google_oauth_client_secret, jwt_signing_key.
# Grants: secretsmanager:GetSecretValue to var.reader_principal_arns.
#
# All three secrets share an identical access pattern (single read action,
# same set of consumers - the ECS task role). Sharing the document keeps
# the access matrix in lockstep and avoids drift.
###############################################################################

data "aws_iam_policy_document" "standard_secret_access" {
  statement {
    sid    = "AllowSecretReadByTaskRole"
    effect = "Allow"

    principals {
      type        = "AWS"
      identifiers = var.reader_principal_arns
    }

    actions = [
      "secretsmanager:GetSecretValue",
    ]

    # The resource is implicit when used as a resource policy on the
    # secret itself; AWS expects either Resource = "*" (the secret it's
    # attached to) or no Resource block at all. We use "*" here per
    # AWS resource-policy convention.
    resources = ["*"]
  }
}

###############################################################################
# DB password access policy document
#
# Attached to: db_password (only).
# Always grants:
#   - secretsmanager:GetSecretValue to var.reader_principal_arns (ECS task role)
# Conditionally grants (when var.enable_db_password_rotation == true AND
# var.rotation_lambda_arn != ""):
#   - secretsmanager:GetSecretValue           }
#   - secretsmanager:PutSecretValue           } to var.rotation_lambda_arn
#   - secretsmanager:UpdateSecretVersionStage }
#   - secretsmanager:DescribeSecret           }
#
# These are the four actions an AWS-provided rotation Lambda needs to:
#   1. Read the current version (GetSecretValue with VersionStage=AWSCURRENT)
#   2. Write the new version (PutSecretValue with VersionStage=AWSPENDING)
#   3. Promote the new version (UpdateSecretVersionStage AWSCURRENT->AWSPREVIOUS)
#   4. Inspect rotation metadata (DescribeSecret)
#
# Reference: AWS docs - "Permissions for rotating secrets"
###############################################################################

data "aws_iam_policy_document" "db_password_access" {
  #############################################################################
  # Statement 1: ECS task role read access (always present)
  #############################################################################
  statement {
    sid    = "AllowSecretReadByTaskRole"
    effect = "Allow"

    principals {
      type        = "AWS"
      identifiers = var.reader_principal_arns
    }

    actions = [
      "secretsmanager:GetSecretValue",
    ]

    resources = ["*"]
  }

  #############################################################################
  # Statement 2: Rotation Lambda read/write access (conditional on
  # var.enable_db_password_rotation && var.rotation_lambda_arn != "")
  #
  # The dynamic "statement" block emits zero or one statement based on the
  # for_each expression. This is Terraform's idiom for conditional statement
  # inclusion in an aws_iam_policy_document: a list with one element when the
  # condition is true, zero elements when false. Without dynamic, a statement
  # with empty principals would generate an invalid IAM policy that AWS
  # rejects.
  #############################################################################
  dynamic "statement" {
    for_each = var.enable_db_password_rotation && var.rotation_lambda_arn != "" ? [1] : []
    content {
      sid    = "AllowRotationLambda"
      effect = "Allow"

      principals {
        type        = "AWS"
        identifiers = [var.rotation_lambda_arn]
      }

      actions = [
        "secretsmanager:GetSecretValue",
        "secretsmanager:PutSecretValue",
        "secretsmanager:UpdateSecretVersionStage",
        "secretsmanager:DescribeSecret",
      ]

      resources = ["*"]
    }
  }
}
