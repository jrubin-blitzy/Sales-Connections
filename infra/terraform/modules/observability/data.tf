###############################################################################
# infra/terraform/modules/observability/data.tf
#
# Data sources for the observability module.
#
# Data sources declared:
#   1. aws_partition.current        - partition-portable ARN construction
#                                     (aws / aws-us-gov / aws-cn)
#   2. aws_region.current           - region context (used for assertions
#                                     and surfaced for callers consuming
#                                     var.region cross-checks)
#   3. aws_caller_identity.current  - AWS account ID for resource ARNs
#                                     in the ALB log delivery bucket policy
#                                     and for the aws:SourceAccount condition
#                                     scoping the service principal
#   4. aws_elb_service_account.current - region-specific AWS account ID
#                                     used by ALB log delivery in older
#                                     regions (legacy delivery mechanism);
#                                     resolved automatically by the AWS
#                                     provider from the well-known per-region
#                                     account ID table
#   5. aws_iam_policy_document.alb_logs_bucket_policy
#                                     - composes the S3 bucket policy
#                                     granting ALB log delivery service
#                                     s3:PutObject (and s3:GetBucketAcl)
#                                     access; consumed by the
#                                     aws_s3_bucket_policy resource in main.tf
#
# ALB log delivery authorization pattern:
#   - Legacy regions: bucket policy grants s3:PutObject to a region-specific
#     AWS account (Principal: AWS = arn:<partition>:iam::<elb_service_acct>:root).
#   - Newer regions: bucket policy grants s3:PutObject to the service
#     logdelivery.elasticloadbalancing.amazonaws.com with aws:SourceAccount
#     condition for tenant isolation.
#
# Both statements are included in the policy so the module works in any
# region. AWS evaluates statements with effect=Allow inclusively (any matching
# statement permits access), so listing both is safe even when only one
# applies in practice.
#
# A third statement grants s3:GetBucketAcl to the service principal because
# ALB log delivery performs a GetBucketAcl call during the access-log
# enablement bootstrap; without it ALB refuses to enable access logging
# even though the PutObject grant succeeds at runtime.
#
# Reference: AWS docs - Access logs for your Application Load Balancer
###############################################################################

###############################################################################
# Partition / region / caller identity
#
# These three "current"-named data sources are the standard provider-
# environment lookups consumed by:
#   - aws_iam_policy_document.alb_logs_bucket_policy below for ARN
#     construction (partition) and the SourceAccount condition (account_id);
#   - sibling Terraform files in this module that reference module-local
#     data.aws_region.current.name for cross-checks against var.region;
#   - future enhancements (e.g., region-aware dashboard widgets, account-
#     scoped alarm dimensions) that need these values without adding
#     additional data sources.
#
# All three are zero-cost lookups: the AWS provider resolves them from the
# already-authenticated session without issuing extra API calls.
###############################################################################

data "aws_partition" "current" {}

data "aws_region" "current" {}

data "aws_caller_identity" "current" {}

###############################################################################
# AWS account ID used by ALB log delivery in legacy regions.
#
# This data source returns the well-known AWS-internal account that delivers
# ALB access logs in regions using the legacy delivery mechanism. AWS
# publishes the per-region account ID table at:
#
#     https://docs.aws.amazon.com/elasticloadbalancing/latest/application/
#     enable-access-logging.html#attach-bucket-policy
#
# The Terraform data source is the abstraction over that table: given the
# region of the configured AWS provider, it returns the matching account ID
# (and pre-formatted IAM root ARN). In newer regions, this account ID is
# unused at runtime (the service principal delivers logs instead), but
# declaring the data source costs nothing and keeps the bucket policy
# region-portable.
###############################################################################

data "aws_elb_service_account" "current" {}

###############################################################################
# S3 bucket policy for ALB access log delivery
#
# Three statements, each effect=Allow:
#
#   Statement 1 (legacy regions): Grant s3:PutObject to the region-specific
#     ALB log delivery AWS account (data.aws_elb_service_account.current).
#     Resource: arn:<partition>:s3:::<bucket>/*
#     Canonical pattern for older regions per AWS docs.
#
#   Statement 2 (newer regions): Grant s3:PutObject to the service principal
#     logdelivery.elasticloadbalancing.amazonaws.com, scoped by
#     aws:SourceAccount equal to the calling account so a malicious cross-
#     account caller cannot trick the service into writing to this bucket.
#
#   Statement 3 (bootstrap): Grant s3:GetBucketAcl to the same service
#     principal so the access-log enablement handshake succeeds on the
#     newer delivery mechanism. Without this statement, ALB refuses to
#     enable access logging even when the PutObject grant is correct.
#
# Bucket-level vs object-level resource ARNs:
#   - s3:PutObject       -> arn:<partition>:s3:::<bucket>/*  (object-level)
#   - s3:GetBucketAcl    -> arn:<partition>:s3:::<bucket>    (bucket-level)
# Mismatching these returns AccessDenied at runtime even though the policy
# parses successfully.
#
# The aws:SourceArn condition is omitted because the ALB ARN is not known
# at policy-creation time (the ALB is provisioned in a sibling module).
# aws:SourceAccount alone is sufficient in the single-account MVP scope;
# adding SourceArn is a post-MVP hardening task tracked in the decision log.
#
# Inputs:
#   - local.alb_logs_bucket_name           : bucket name (from locals.tf)
#   - data.aws_partition.current.partition : partition for ARN construction
#   - data.aws_caller_identity.current.account_id : SourceAccount condition
#   - data.aws_elb_service_account.current.arn : legacy-mechanism principal
#
# Output:
#   - data.aws_iam_policy_document.alb_logs_bucket_policy.json : the policy
#     JSON consumed by aws_s3_bucket_policy.alb_logs in main.tf.
###############################################################################

data "aws_iam_policy_document" "alb_logs_bucket_policy" {
  #############################################################################
  # Statement 1: Legacy region delivery (AWS-account principal)
  #
  # In legacy regions, ALB log delivery operates from a region-specific AWS
  # account whose ID is published by AWS. The principal is expressed as the
  # IAM root ARN of that account (data.aws_elb_service_account.current.arn,
  # which evaluates to "arn:<partition>:iam::<account-id>:root").
  #############################################################################
  statement {
    sid    = "AllowELBAccountPutObject"
    effect = "Allow"

    principals {
      type        = "AWS"
      identifiers = [data.aws_elb_service_account.current.arn]
    }

    actions = [
      "s3:PutObject",
    ]

    resources = [
      "arn:${data.aws_partition.current.partition}:s3:::${local.alb_logs_bucket_name}/*",
    ]
  }

  #############################################################################
  # Statement 2: Newer region delivery (service principal)
  #
  # In post-2022 regions, the ALB log delivery service uses an AWS service
  # principal rather than a per-region account ID. The aws:SourceAccount
  # condition restricts the grant so that only ALBs in our own AWS account
  # can drive log delivery to this bucket - protection against cross-account
  # "log poisoning" where another tenant could trick the service into
  # writing to our bucket.
  #############################################################################
  statement {
    sid    = "AllowLogDeliveryServicePutObject"
    effect = "Allow"

    principals {
      type        = "Service"
      identifiers = ["logdelivery.elasticloadbalancing.amazonaws.com"]
    }

    actions = [
      "s3:PutObject",
    ]

    resources = [
      "arn:${data.aws_partition.current.partition}:s3:::${local.alb_logs_bucket_name}/*",
    ]

    condition {
      test     = "StringEquals"
      variable = "aws:SourceAccount"
      values   = [data.aws_caller_identity.current.account_id]
    }
  }

  #############################################################################
  # Statement 3: Allow ELB log delivery service to verify bucket ACL
  #
  # ALB log delivery performs a GetBucketAcl call during the access-log
  # enablement bootstrap to verify the bucket can receive logs. Without
  # this grant, the bootstrap fails and ALB refuses to enable access
  # logging even though the PutObject grant in Statement 2 is correct.
  #
  # The resource ARN is bucket-level (no /* suffix) because GetBucketAcl
  # operates on the bucket itself, not on individual objects within it.
  #############################################################################
  statement {
    sid    = "AllowELBLogDeliveryGetBucketAcl"
    effect = "Allow"

    principals {
      type        = "Service"
      identifiers = ["logdelivery.elasticloadbalancing.amazonaws.com"]
    }

    actions = [
      "s3:GetBucketAcl",
    ]

    resources = [
      "arn:${data.aws_partition.current.partition}:s3:::${local.alb_logs_bucket_name}",
    ]

    condition {
      test     = "StringEquals"
      variable = "aws:SourceAccount"
      values   = [data.aws_caller_identity.current.account_id]
    }
  }
}
