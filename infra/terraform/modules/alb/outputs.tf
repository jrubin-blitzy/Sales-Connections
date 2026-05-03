###############################################################################
# infra/terraform/modules/alb/outputs.tf
#
# Outputs surfaced by the ALB module. Consumed by:
#   - infra/terraform/main.tf (root composition module wiring)
#   - infra/terraform/outputs.tf (re-exported as root composition outputs)
#   - infra/terraform/modules/ecs/ (target_group_arn for service registration,
#                                    security_group_id for SG cross-references)
#   - infra/terraform/modules/observability/ (arn_suffix, target_group_arn_suffix
#                                              for CloudWatch alarm dimensions)
#
# These outputs are the public CONTRACT of the ALB module. Renaming any
# output here is a breaking change to the entire Terraform composition.
#
# CRITICAL SECURITY INVARIANT (AAP Sec 0.7.4):
#   This module produces no secret values. Outputs surface only ARNs,
#   DNS names, zone IDs, and security group IDs that point to provisioned
#   AWS resources. None of these are secrets per se - they appear in
#   CloudTrail, IAM policies, and Terraform state.
#
# Convention:
#   - Every output declares description (mandatory per Authoring Conventions).
#   - No sensitive = true markers (ARNs/DNS/IDs are not secrets).
#   - All outputs reference resources defined in main.tf or locals.tf.
###############################################################################

###############################################################################
# Application Load Balancer DNS and routing
###############################################################################

output "dns_name" {
  description = "Public DNS name of the ALB (e.g., 'sales-connections-prod-1234567890.us-east-1.elb.amazonaws.com'). The application is reachable at https://<dns_name> when no custom domain is configured. Operations runbooks document CNAME-ing this to the application's domain via Route53 alias records out-of-band."
  value       = aws_lb.main.dns_name
}

output "zone_id" {
  description = "Hosted zone ID of the ALB. Required for Route53 alias records (when domain_name is configured); used as the alias_target.zone_id in the root composition's optional Route53 module."
  value       = aws_lb.main.zone_id
}

###############################################################################
# Application Load Balancer ARNs
#
# arn vs arn_suffix:
#   - arn:        full ARN, used in IAM policies and resource references
#                 (e.g., 'arn:aws:elasticloadbalancing:us-east-1:123:loadbalancer/app/foo/abc')
#   - arn_suffix: the trailing portion AWS uses as a CloudWatch metric
#                 dimension (e.g., 'app/foo/abc'). Required by the
#                 AWS/ApplicationELB CloudWatch namespace.
###############################################################################

output "arn" {
  description = "Full ARN of the Application Load Balancer. Used in IAM policy resource scoping (e.g., listener-create permissions) and for cross-module reference (e.g., observability alarms that scope to a specific ALB)."
  value       = aws_lb.main.arn
}

output "arn_suffix" {
  description = "ARN suffix of the ALB used as a CloudWatch metric dimension under the AWS/ApplicationELB namespace. Format is 'app/<lb-name>/<lb-id>'. Consumed by modules/observability/ for HTTPCode_ELB_5XX_Count, TargetResponseTime, and similar load-balancer-scoped metric alarms."
  value       = aws_lb.main.arn_suffix
}

###############################################################################
# Listener ARNs
#
# Listener ARNs are surfaced for:
#   - Future listener-rule additions (e.g., adding a /api/v2 path-routing rule)
#   - Operations runbooks describing how to inspect or modify listener config
#   - CloudWatch alarms that scope to a specific listener (rare; usually
#     metrics aggregate at the LB level)
###############################################################################

output "listener_https_arn" {
  description = "ARN of the HTTPS:443 listener (TLS termination via ACM). Surfaced for future listener-rule additions and for diagnostic reference. The default action forwards all traffic to target_group_arn."
  value       = aws_lb_listener.https.arn
}

output "listener_http_arn" {
  description = "ARN of the HTTP:80 listener (redirect to HTTPS:443 with HTTP_301). Surfaced for diagnostic reference. The default action is a redirect; no forwarding rules should be added without violating the HTTPS-only invariant."
  value       = aws_lb_listener.http.arn
}

###############################################################################
# Target Group ARNs
#
# Backend target group registers the ECS Fargate service via IP-mode targets.
###############################################################################

output "target_group_arn" {
  description = "ARN of the backend target group (target_type = 'ip' for Fargate compatibility). Consumed by modules/ecs/ to register the backend service via the aws_ecs_service.load_balancer block. Health checks on this target group hit backend_health_check_path (default /healthz)."
  value       = aws_lb_target_group.backend.arn
}

output "target_group_arn_suffix" {
  description = "ARN suffix of the backend target group used as a CloudWatch metric dimension under the AWS/ApplicationELB namespace. Format is 'targetgroup/<tg-name>/<tg-id>'. Consumed by modules/observability/ for UnHealthyHostCount, HealthyHostCount, RequestCount, and similar target-group-scoped metric alarms."
  value       = aws_lb_target_group.backend.arn_suffix
}

output "target_group_name" {
  description = "Name of the backend target group. Surfaced for diagnostic reference and IAM policy authoring."
  value       = aws_lb_target_group.backend.name
}

###############################################################################
# Security Group outputs
###############################################################################

output "security_group_id" {
  description = "ID of the ALB security group. Consumed by modules/ecs/ as the source SG for tasks_ingress_from_alb (allowing ALB-to-tasks traffic on the backend container port). The ALB SG itself accepts public 80/443 ingress from var.allowed_ingress_cidrs."
  value       = aws_security_group.alb.id
}

output "security_group_arn" {
  description = "ARN of the ALB security group. Surfaced for IAM policies that require ARN-level scoping (e.g., AWS Network Firewall rule references)."
  value       = aws_security_group.alb.arn
}

###############################################################################
# ACM Certificate output
#
# When var.acm_certificate_arn is supplied (existing cert), this output
# returns that value. When var.acm_certificate_arn is empty AND a domain
# is configured, this returns the freshly provisioned aws_acm_certificate.main[0].arn.
# When neither, this returns "" (the HTTPS listener will fail at apply time;
# operators must supply either an ARN or a domain).
###############################################################################

output "acm_certificate_arn" {
  description = "ARN of the ACM certificate attached to the HTTPS listener. Returns the externally-supplied var.acm_certificate_arn when provided, OR the freshly provisioned cert ARN when var.acm_certificate_arn is empty and var.domain_name is set, OR an empty string when neither is configured (which will fail at apply time on the HTTPS listener)."
  value       = local.effective_certificate_arn
}
