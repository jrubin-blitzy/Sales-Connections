###############################################################################
# infra/terraform/modules/alb/acm.tf
#
# ACM (AWS Certificate Manager) certificate provisioning for the ALB HTTPS
# listener.
#
# Conditional behavior:
#   - var.acm_certificate_arn != "":  Use the supplied existing cert ARN.
#                                     This file's resources count = 0.
#   - var.acm_certificate_arn == ""
#     AND var.domain_name != "":      Provision a new cert via ACM with
#                                     DNS validation method.
#   - Both empty:                     No cert provisioned. The HTTPS
#                                     listener's lifecycle.precondition
#                                     in main.tf will fail at plan time.
#
# Resources:
#   1. aws_acm_certificate.main[0]
#        - domain_name = var.domain_name (primary)
#        - subject_alternative_names = var.alternative_domain_names
#        - validation_method = "DNS"
#
#   2. aws_acm_certificate_validation.main[0]
#        - Conditional on var.wait_for_certificate_validation = true
#        - Validates that DNS records have been created (out-of-band or
#          via a future modules/route53/)
#        - When NOT enabled, terraform apply does NOT block on validation;
#          operators must verify the cert is ISSUED before deploying.
#
# CRITICAL CONSTRAINTS (from AAP and folder spec):
#   - validation_method = "DNS" (NEVER "EMAIL" - email validation is
#     unsupported for AWS-managed automation).
#   - Cert provisioning happens in the same region as the ALB (data sources
#     in data.tf resolve to data.aws_region.current.name implicitly via
#     provider configuration).
#   - For CloudFront-fronted SPA delivery (post-MVP), a separate cert in
#     us-east-1 would be required, NOT provisioned here.
###############################################################################

###############################################################################
# ACM Certificate (Conditional)
#
# Provisioned ONLY when:
#   var.acm_certificate_arn == "" AND var.domain_name != ""
#
# Encoded as local.provision_acm_certificate (defined in locals.tf).
#
# DNS validation method: AWS publishes a CNAME record token; the operator
# (or a future modules/route53/) creates the corresponding CNAME in DNS;
# AWS detects the record and issues the cert. This is the canonical
# automation-friendly validation path.
#
# create_before_destroy on the cert resource lets us rotate certs without
# downtime: the new cert is issued and attached to the listener before the
# old one is detached.
#
# key_algorithm defaults to RSA_2048 (acceptable; ECDSA_P384 would be
# marginally faster but RSA is universally supported and matches the
# default recommended by AWS for ACM certs).
###############################################################################

resource "aws_acm_certificate" "main" {
  count = local.provision_acm_certificate ? 1 : 0

  domain_name               = var.domain_name
  subject_alternative_names = var.alternative_domain_names
  validation_method         = "DNS"

  tags = merge(local.module_tags, {
    Name = "${var.name_prefix}-acm-cert"
  })

  lifecycle {
    create_before_destroy = true
  }
}

###############################################################################
# ACM Certificate Validation (Conditional)
#
# Triggers DNS validation completion. Provisioned ONLY when:
#   - local.provision_acm_certificate is true (cert is being created here), AND
#   - var.wait_for_certificate_validation is true (operator confirms DNS
#     records exist or will be created out-of-band)
#
# When wait_for_certificate_validation is false (default), terraform apply
# completes WITHOUT waiting for the cert to be ISSUED. The HTTPS listener
# in main.tf will reference the cert ARN; if the cert is still in
# PENDING_VALIDATION state at the time of listener creation, AWS will
# reject the listener creation. Operators must therefore:
#   1. Run terraform apply (cert is requested, validation is not enforced).
#   2. Create DNS validation CNAME records out-of-band.
#   3. Wait for cert status = ISSUED.
#   4. Run terraform apply again (now the listener creation succeeds).
#
# When wait_for_certificate_validation is true, terraform apply BLOCKS at
# this resource until validation completes (typically 5-30 minutes after
# DNS records are created). This is the ideal behavior when a future
# modules/route53/ exists to automate the DNS records.
#
# validation_record_fqdns is intentionally OMITTED here because we do NOT
# provision DNS validation records in this module (per AAP Sec 0.4.9:
# "No Route53 records"). AWS auto-detects DNS validation records when they
# exist in any DNS provider, so omitting the argument is correct here.
# When a future modules/route53/ creates the validation records in the
# same plan, this resource will need an explicit validation_record_fqdns
# list pointing at those records' FQDNs (so Terraform sequences cert
# validation after the records exist).
#
# timeouts.create = "45m": AWS docs recommend allowing 30+ minutes for DNS
# propagation and validation. 45 minutes is generous and prevents
# premature failure on slow DNS resolvers.
###############################################################################

resource "aws_acm_certificate_validation" "main" {
  count = local.provision_acm_certificate && var.wait_for_certificate_validation ? 1 : 0

  certificate_arn = aws_acm_certificate.main[0].arn

  timeouts {
    create = "45m"
  }
}
