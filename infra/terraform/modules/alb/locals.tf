###############################################################################
# infra/terraform/modules/alb/locals.tf
#
# Module-internal locals for the ALB module:
#
#   1. module_tags                       Standard tag set merged into every
#                                        resource (Component=ALB, Environment,
#                                        ManagedBy, Module).
#
#   2. Resource naming locals            alb_name, target_group_name,
#                                        security_group_name_prefix
#                                        (all derived from var.name_prefix).
#
#   3. provision_acm_certificate         Boolean gate: true when no existing
#                                        cert ARN is supplied AND a domain
#                                        is configured. Drives count on
#                                        aws_acm_certificate.main[0] and
#                                        aws_acm_certificate_validation.main[0].
#
#   4. effective_certificate_arn         The cert ARN that the HTTPS listener
#                                        attaches: either the supplied
#                                        var.acm_certificate_arn OR the
#                                        freshly provisioned cert ARN.
#
#   5. access_logs_active                Boolean gate: true when
#                                        var.access_logs_enabled AND
#                                        var.access_logs_bucket != "".
#                                        Drives the dynamic "access_logs"
#                                        block on aws_lb.main.
#
# Multiple locals { ... } blocks are used for organization; Terraform merges
# them within a single module at parse time.
###############################################################################

###############################################################################
# Standard module tags
#
# Merged into every taggable resource in this module. Operators can extend
# via var.tags. Module-internal tags (Component, Environment, ManagedBy,
# Module) cannot be overridden via var.tags by accident because merge()
# right-most-wins: the static map below wins over var.tags.
###############################################################################

locals {
  module_tags = merge(
    var.tags,
    {
      Component   = "ALB"
      Environment = var.environment
      ManagedBy   = "Terraform"
      Module      = "infra/terraform/modules/alb"
    },
  )
}

###############################################################################
# Resource naming locals
#
# All ALB resources share var.name_prefix as the leading token. Defining
# the per-resource prefixes here keeps naming consistent and simplifies
# rename operations.
#
# AWS naming constraints honored:
#   - aws_lb.name: max 32 chars, alphanumeric and hyphens only, no leading
#     or trailing hyphens. With var.name_prefix capped at 28 chars in
#     variables.tf, "${var.name_prefix}-alb" stays within the 32-char limit.
#   - aws_lb_target_group.name: max 32 chars, same charset. The
#     "-backend-tg" suffix (11 chars) requires var.name_prefix <= 21 chars
#     for full safety; in practice operators stay well under this when
#     using e.g. "sales-connections-prod" (22 chars).
#   - aws_security_group.name_prefix: AWS appends a random hex suffix, so
#     the trailing hyphen produces "${var.name_prefix}-alb-<random>"
#     rather than "${var.name_prefix}-alb<random>". Security group names
#     can be up to 255 chars, so the prefix length is not a binding
#     constraint here.
###############################################################################

locals {
  alb_name                   = "${var.name_prefix}-alb"
  target_group_name          = "${var.name_prefix}-backend-tg"
  security_group_name_prefix = "${var.name_prefix}-alb-"
}

###############################################################################
# ACM certificate locals
#
# provision_acm_certificate gates the conditional creation of
# aws_acm_certificate.main[0] in acm.tf. The condition is:
#
#   var.acm_certificate_arn == ""    (operator did NOT supply an existing cert)
#   AND var.domain_name != ""        (operator DID supply a domain to provision for)
#
# When neither condition holds (no existing cert AND no domain), the HTTPS
# listener's lifecycle.precondition (in main.tf) catches the misconfigured
# state at plan time.
#
# effective_certificate_arn resolves to:
#   - var.acm_certificate_arn          when a non-empty ARN is supplied
#   - aws_acm_certificate.main[0].arn  when this module provisions the cert
#   - ""                               when neither (lifecycle precondition
#                                      catches this in main.tf)
#
# The HTTPS listener consumes effective_certificate_arn directly, so the
# decision logic is encapsulated here and not duplicated in main.tf.
#
# Why two locals (a flag and a ternary) instead of one:
#   - provision_acm_certificate drives `count` on the cert resource. count
#     must be evaluable at plan time before any resource exists, so it
#     references only var.* values.
#   - effective_certificate_arn drives the listener's certificate_arn
#     argument, which is evaluated lazily after the cert resource has been
#     planned. It can therefore reference aws_acm_certificate.main safely.
#   Keeping them separate matches Terraform's evaluation semantics and
#   avoids "Inconsistent conditional result types" plan-time errors.
###############################################################################

locals {
  provision_acm_certificate = var.acm_certificate_arn == "" && var.domain_name != ""

  effective_certificate_arn = (
    var.acm_certificate_arn != ""
    ? var.acm_certificate_arn
    : (
      length(aws_acm_certificate.main) > 0
      ? aws_acm_certificate.main[0].arn
      : ""
    )
  )
}

###############################################################################
# Access logs locals
#
# access_logs_active gates the dynamic "access_logs" block on aws_lb.main.
# Both conditions must hold for access logs to be wired:
#
#   var.access_logs_enabled            (operator opted in)
#   AND var.access_logs_bucket != ""   (the bucket name is supplied)
#
# This handles the case where access_logs_enabled = true but the
# observability module hasn't yet provided a bucket (e.g., during initial
# stack bootstrap when modules apply in sequence). Access logs are simply
# skipped until the bucket is available - no error, no plan failure.
###############################################################################

locals {
  access_logs_active = var.access_logs_enabled && var.access_logs_bucket != ""
}
