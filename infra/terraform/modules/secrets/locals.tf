###############################################################################
# infra/terraform/modules/secrets/locals.tf
#
# Module-internal locals derived from input variables. Centralized here so
# main.tf and data.tf consume canonical expressions rather than duplicating
# the conditional logic.
#
# Locals exposed:
#   - secret_names           : map of friendly identifier -> path-style
#                              Secrets Manager name (e.g.,
#                              "${var.name_prefix}/anthropic_api_key").
#                              Used by main.tf for `name = ...` and `Name`
#                              tags, and by outputs.tf for the secret_names
#                              output map.
#   - placeholder_value      : literal seed value written by Terraform when
#                              var.write_placeholder_versions is true.
#                              Constant: "placeholder-replace-via-cli" per
#                              the folder spec.
#   - kms_key_id_or_null     : null when var.kms_key_id is empty (use
#                              AWS-managed key); otherwise the value.
#                              Required because aws_secretsmanager_secret
#                              rejects an empty-string kms_key_id.
#   - module_tags            : merge(var.tags, { Component = "Secrets",
#                                                Environment = var.environment }).
#                              Used by every secret resource via
#                              merge(local.module_tags, ...).
#
# Locals here are pure functions of input variables (no resource references,
# no data sources). This means terraform plan evaluates them at parse time
# without an AWS API call, which gives fast feedback for input validation.
###############################################################################

locals {
  #############################################################################
  # Secret naming (path-based hierarchy)
  #
  # Per folder spec: "All secrets follow the naming convention
  # ${name_prefix}/<secret_name> (slash separator for path-based hierarchy
  # in the Secrets Manager console)."
  #
  # The Secrets Manager console renders slash-separated names as a
  # navigable tree, so all four secrets cluster under the same name_prefix
  # path. This dramatically improves discoverability for operators.
  #
  # Used by:
  #   - aws_secretsmanager_secret.anthropic_api_key.name
  #   - aws_secretsmanager_secret.google_oauth_client_secret.name
  #   - aws_secretsmanager_secret.jwt_signing_key.name
  #   - aws_secretsmanager_secret.db_password.name
  #   - random_password.X.keepers.secret_name
  #   - outputs.tf -> secret_names map
  #############################################################################
  secret_names = {
    anthropic_api_key          = "${var.name_prefix}/anthropic_api_key"
    google_oauth_client_secret = "${var.name_prefix}/google_oauth_client_secret"
    jwt_signing_key            = "${var.name_prefix}/jwt_signing_key"
    db_password                = "${var.name_prefix}/db_password"
  }

  #############################################################################
  # Placeholder seed value (dev / pre-population stub)
  #
  # Per folder spec critical constraints:
  #   "Dev/Staging: Terraform generates placeholder values via
  #    random_password or writes literal 'placeholder-replace-via-cli'"
  #
  # This literal is written as the initial secret_string when
  # var.write_placeholder_versions is true AND the secret has no
  # Terraform-generated value (i.e., not jwt with generate_jwt_key=true and
  # not db_password with generate_db_password=true).
  #
  # Once an operator populates the real value via:
  #   aws secretsmanager put-secret-value --secret-id <name> --secret-string ...
  # the lifecycle { ignore_changes = [secret_string] } block on the version
  # resource prevents Terraform from reverting to this placeholder on
  # subsequent applies.
  #
  # IMPORTANT: This value is INTENTIONALLY a meaningful, recognizable
  # string rather than e.g. an empty string or random data. Operators
  # encountering it know immediately that the secret has not yet been
  # populated with the real credential. This is a deliberate UX choice
  # for incident-response clarity ("why isn't the API working?" -> read
  # the secret value -> see the placeholder -> populate the real value).
  #############################################################################
  placeholder_value = "placeholder-replace-via-cli"

  #############################################################################
  # KMS key ID coercion
  #
  # The aws_secretsmanager_secret resource's kms_key_id argument accepts:
  #   - null   -> use the AWS-managed key (alias/aws/secretsmanager)
  #   - <arn>  -> use the customer-managed CMK at that ARN
  #
  # An empty string ("") is REJECTED by the provider with a validation
  # error. We coerce empty to null here so callers can pass var.kms_key_id
  # = "" to mean "use AWS-managed key" - which matches the empty-string
  # default in variables.tf.
  #
  # Used by every aws_secretsmanager_secret resource's kms_key_id argument.
  #############################################################################
  kms_key_id_or_null = var.kms_key_id != "" ? var.kms_key_id : null

  #############################################################################
  # Tag merge: caller-provided var.tags + module-specific labels
  #
  # Per the folder spec authoring convention: "All resources accept and
  # merge var.tags plus module-specific labels". Here we layer in:
  #   - Component   = "Secrets"   (per folder spec implicit tag)
  #   - Environment = var.environment (for cost-explorer drill-down)
  #
  # Each secret resource further layers in resource-specific labels
  # (SecretType, Service, Name) via merge(local.module_tags, { ... })
  # at the resource site.
  #
  # var.tags is the BASE in the merge() call so module-injected labels
  # (Component, Environment) take precedence over caller-supplied keys
  # with the same name - the module's identity tag must not be overrideable.
  #
  # Used by every aws_secretsmanager_secret resource via
  # merge(local.module_tags, ...).
  #############################################################################
  module_tags = merge(
    var.tags,
    {
      Component   = "Secrets"
      Environment = var.environment
    },
  )
}
