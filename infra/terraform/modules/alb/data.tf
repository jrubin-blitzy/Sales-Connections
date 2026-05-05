###############################################################################
# infra/terraform/modules/alb/data.tf
#
# Data sources consumed by the ALB module. Centralizing them here (rather
# than co-locating with consuming resources) follows the pattern established
# in sibling modules (network, database, ecs, secrets, ecr).
#
# Categories:
#   1. Provider environment lookups (partition, region, account)
#
# OUT OF SCOPE per AAP Sec 0.4.9:
#   - aws_route53_zone (DNS managed externally)
#   - aws_route53_record for ACM validation (records created out-of-band)
###############################################################################

###############################################################################
# Provider environment lookups
#
# Used for:
#   - data.aws_partition.current.partition: portable IAM/ARN construction
#     across aws, aws-us-gov, and aws-cn partitions. Future use cases include
#     listener-rule resource policies and access-log bucket policies.
#
#   - data.aws_region.current.name: surfaced for operations runbook output,
#     log format strings, and (if added later) CloudWatch alarm dimensions.
#
#   - data.aws_caller_identity.current.account_id: surfaced for forensic
#     annotations and resource policy construction. The ALB module currently
#     doesn't author resource policies, but the data source is declared for
#     consistency with sibling modules.
###############################################################################

data "aws_partition" "current" {}

data "aws_region" "current" {}

data "aws_caller_identity" "current" {}
