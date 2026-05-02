###############################################################################
# infra/terraform/modules/database/locals.tf
#
# Module-internal locals derived from input variables. Centralized here so
# main.tf consumes canonical expressions rather than duplicating the
# conditional logic.
#
# Locals exposed:
#   - engine_version_major     : major version extracted from var.engine_version
#                                (e.g., "17" from "17.7")
#   - parameter_group_family   : "postgres${engine_version_major}"
#                                (e.g., "postgres17")
#   - kms_key_id_or_null       : null when var.kms_key_id is empty (use
#                                AWS-managed alias/aws/rds key); otherwise
#                                the value. Required because
#                                aws_db_instance rejects an empty-string
#                                kms_key_id.
#   - module_tags              : merge(var.tags, { Component = "Database",
#                                                  Environment = ... })
#                                Used by every resource via
#                                merge(local.module_tags, ...).
#
# Locals here are pure functions of input variables (no resource references,
# no data sources). This means terraform plan evaluates them at parse time
# without an AWS API call, which gives fast feedback for input validation.
###############################################################################

locals {
  #############################################################################
  # PostgreSQL major version extraction
  #
  # var.engine_version is the full version string (e.g., "17.7"). The DB
  # parameter group family argument requires just the major version
  # (e.g., "17"). Extract via split() on the dot separator.
  #
  # Used by:
  #   - local.parameter_group_family
  #   - aws_db_parameter_group.main.name (composed as "...-postgres17")
  #############################################################################
  engine_version_major = split(".", var.engine_version)[0]

  #############################################################################
  # DB parameter group family
  #
  # AWS expects the form "postgresN" where N is the major version. This is
  # derived from local.engine_version_major so a future engine_version
  # override (e.g., "17.8" or "18.0") automatically picks the correct
  # family without editing this file.
  #
  # Used by:
  #   - aws_db_parameter_group.main.family
  #############################################################################
  parameter_group_family = "postgres${local.engine_version_major}"

  #############################################################################
  # KMS key ID coercion
  #
  # The aws_db_instance resource's kms_key_id argument accepts:
  #   - null   -> use the AWS-managed key (alias/aws/rds)
  #   - <arn>  -> use the customer-managed CMK at that ARN
  #
  # An empty string ("") is REJECTED by the provider with a validation
  # error. We coerce empty to null here so callers can pass var.kms_key_id
  # = "" to mean "use AWS-managed key" - which matches the empty-string
  # default in variables.tf.
  #
  # Used by:
  #   - aws_db_instance.main.kms_key_id
  #############################################################################
  kms_key_id_or_null = var.kms_key_id != "" ? var.kms_key_id : null

  #############################################################################
  # Tag merge: caller-provided var.tags + module-specific labels
  #
  # Per the folder spec authoring convention: "Every resource merges
  # var.tags and adds module-specific labels (e.g., Component = 'Database')".
  # Here we layer in:
  #   - Component   = "Database"   (per folder spec critical constraint)
  #   - Environment = var.environment (for cost-explorer drill-down)
  #
  # Each resource further layers in resource-specific labels (Name)
  # via merge(local.module_tags, { Name = "..." }) at the resource site.
  #
  # var.tags is the BASE in the merge() call so module-injected labels
  # (Component, Environment) take precedence over caller-supplied keys
  # with the same name - the module's identity tag must not be overrideable.
  #
  # Used by every resource in main.tf via merge(local.module_tags, ...).
  #############################################################################
  module_tags = merge(
    var.tags,
    {
      Component   = "Database"
      Environment = var.environment
    },
  )
}
