###############################################################################
# infra/terraform/modules/database/data.tf
#
# Data sources for the database module.
#
# Data sources declared:
#   1. aws_secretsmanager_secret_version.master_password
#        Reads the RDS master password value from the Secrets Manager entry
#        whose ARN is supplied via var.master_password_secret_arn. The value
#        flows into aws_db_instance.main.password in main.tf and is marked
#        sensitive by Terraform (never appears in plan output or state in
#        plaintext).
#
#   2. aws_partition.current
#        Returns the AWS partition (aws / aws-us-gov / aws-cn) for the
#        current account. Used in main.tf to construct the AWS-managed
#        policy ARN for AmazonRDSEnhancedMonitoringRole portably across
#        partitions.
#
# CRITICAL SECURITY INVARIANT (per AAP Sec 0.4.6 / 0.7.4 and folder spec):
#   The master password is read from Secrets Manager via this data source;
#   it is NEVER hardcoded in any .tf file or .tfvars file. The
#   modules/secrets/ module manages the Secrets Manager entry; this module
#   only READS the current value at apply time.
#
#   Combined with lifecycle { ignore_changes = [password] } on the RDS
#   instance (in main.tf), this allows the password to be:
#     - Initially seeded by Terraform (when var.generate_db_password = true
#       in the secrets module).
#     - Subsequently rotated by an external Lambda or operator action via
#       'aws secretsmanager put-secret-value' (Terraform will read the new
#       value on next apply but ignore_changes prevents revert attempts).
###############################################################################

###############################################################################
# Master password from Secrets Manager
#
# Reads the value of the Secrets Manager entry whose ARN is supplied via
# var.master_password_secret_arn (typically module.secrets.db_password_arn
# in the root composition).
#
# Behaviour:
#   - The data source executes at plan time (and is refreshed at apply time).
#   - secret_string is automatically marked sensitive by Terraform; it
#     never appears in plan output or state file in plaintext.
#   - When the secret value is unset (no version exists) or the entry is
#     not found, plan fails with a clear error message - this is the
#     desired behaviour because RDS provisioning would fail at apply time
#     anyway with a worse error.
#
# Argument notes:
#   - secret_id accepts either the secret ARN or the secret name; we pass
#     the ARN (var.master_password_secret_arn) for unambiguous resolution
#     across accounts and regions.
#   - version_id is omitted on purpose: omission selects the AWSCURRENT
#     stage (the latest active version), which is exactly what we want.
#     Pinning a specific version_id would freeze Terraform on a stale
#     value if the password is rotated by an external Lambda or operator.
#   - version_stage is omitted on purpose: the default "AWSCURRENT" stage
#     matches our rotation model.
#
# Consumed by:
#   - aws_db_instance.main.password (in main.tf)
###############################################################################

data "aws_secretsmanager_secret_version" "master_password" {
  secret_id = var.master_password_secret_arn
}

###############################################################################
# AWS partition (aws / aws-us-gov / aws-cn)
#
# Used in main.tf to construct the AWS-managed policy ARN for
# AmazonRDSEnhancedMonitoringRole:
#
#   "arn:${data.aws_partition.current.partition}:iam::aws:policy/service-role/AmazonRDSEnhancedMonitoringRole"
#
# Hardcoding "arn:aws:..." would break in GovCloud and China partitions.
#
# This data source performs no AWS API call: the AWS provider derives the
# partition from the configured region locally. Including it costs nothing
# and makes the module portable across the standard, GovCloud, and China
# partitions without modification.
###############################################################################

data "aws_partition" "current" {}
