###############################################################################
# infra/terraform/modules/alb/main.tf
#
# Public-facing Application Load Balancer (ALB) for Sales-Connections:
#
#   1. aws_security_group.alb                          (ALB SG)
#   2. aws_security_group_rule.alb_ingress_http        (public 80 ingress)
#   3. aws_security_group_rule.alb_ingress_https       (public 443 ingress)
#   4. aws_security_group_rule.alb_egress_to_ecs       (egress to ECS tasks SG)
#   5. aws_security_group_rule.ecs_ingress_from_alb    (ingress on ECS SG)
#   6. aws_lb.main                                     (Application Load Balancer)
#   7. aws_lb_target_group.backend                     (IP-mode target group)
#   8. aws_lb_listener.https                           (HTTPS:443 with ACM)
#   9. aws_lb_listener.http                            (HTTP:80 -> HTTPS:443)
#
# ACM certificate provisioning (conditional on var.acm_certificate_arn == "")
# lives in acm.tf for readability per the folder spec optional-files note.
#
# CRITICAL CONSTRAINTS (from AAP and folder spec):
#   - TLS via ACM REQUIRED on HTTPS listener.
#   - HTTPS-only: HTTP:80 ONLY redirects to 443.
#   - SSL policy defaults to TLS 1.2+ (TLS 1.3 preferred).
#   - drop_invalid_header_fields = true (header smuggling defense).
#   - enable_http2 = true (SPA performance).
#   - target_type = "ip" (mandatory for Fargate).
#   - Backend tasks have no public IPs; only the ALB is Internet-facing.
#
# CIRCULAR-SG-DEPENDENCY NOTE:
#   The ALB module owns BOTH the alb->ecs egress rule AND the ecs<-alb
#   ingress rule. Per the folder spec critical-requirement note: "placed
#   here to break circular dependencies; alternative is to place it in
#   modules/ecs/". Owning both rules in the ALB module avoids the
#   module-level cycle that would arise if ecs took alb_security_group_id
#   as input AND alb took ecs_security_group_id as input.
#
# Resource graph dependency chain (within this file):
#
#   aws_security_group.alb
#     |-> aws_security_group_rule.alb_ingress_http
#     |-> aws_security_group_rule.alb_ingress_https
#     |-> aws_security_group_rule.alb_egress_to_ecs
#     |-> aws_security_group_rule.ecs_ingress_from_alb
#     |-> aws_lb.main.security_groups
#
#   aws_lb.main
#     |-> aws_lb_target_group.backend.vpc_id (via var.vpc_id, not LB)
#     |-> aws_lb_listener.https.load_balancer_arn
#     |-> aws_lb_listener.http.load_balancer_arn
#
#   aws_acm_certificate.main[0] (defined in acm.tf, conditional)
#     |-> local.effective_certificate_arn (in locals.tf)
#         |-> aws_lb_listener.https.certificate_arn
###############################################################################

###############################################################################
# ALB Security Group
#
# The ALB SG controls public ingress on ports 80 and 443. Egress is restricted
# to the ECS tasks SG on the backend container port (default 8000) - defense
# in depth even though ALB-to-private-subnet traffic is already constrained
# by VPC routing.
#
# name_prefix (vs name) is used so the AWS provider generates a unique suffix;
# combined with create_before_destroy, this allows the SG to be replaced
# without name collision.
###############################################################################

resource "aws_security_group" "alb" {
  name_prefix = local.security_group_name_prefix
  description = "ALB SG: public 80/443 ingress; egress to ECS task SG on backend port"
  vpc_id      = var.vpc_id

  tags = merge(local.module_tags, {
    Name = "${var.name_prefix}-alb-sg"
  })

  lifecycle {
    create_before_destroy = true
  }
}

###############################################################################
# ALB SG Ingress Rules
#
# Public ingress on ports 80 and 443 from var.allowed_ingress_cidrs (default
# ["0.0.0.0/0"]). Operators may tighten this for environments behind a
# corporate WAF (e.g., dev/staging restricted to office IPs).
###############################################################################

resource "aws_security_group_rule" "alb_ingress_http" {
  type              = "ingress"
  description       = "HTTP (80) from public Internet (redirected to HTTPS by listener)"
  from_port         = 80
  to_port           = 80
  protocol          = "tcp"
  security_group_id = aws_security_group.alb.id
  cidr_blocks       = var.allowed_ingress_cidrs
}

resource "aws_security_group_rule" "alb_ingress_https" {
  type              = "ingress"
  description       = "HTTPS (443) from public Internet (TLS termination at ALB via ACM)"
  from_port         = 443
  to_port           = 443
  protocol          = "tcp"
  security_group_id = aws_security_group.alb.id
  cidr_blocks       = var.allowed_ingress_cidrs
}

###############################################################################
# ALB SG Egress Rule
#
# Egress restricted to the ECS tasks SG on the backend container port. This
# is more restrictive than the AWS-default permissive egress: the ALB cannot
# reach anything other than the ECS tasks on the backend port.
#
# When var.allowed_ingress_cidrs deviates from the default (e.g., adding
# additional service ports), update accordingly.
###############################################################################

resource "aws_security_group_rule" "alb_egress_to_ecs" {
  type                     = "egress"
  description              = "ALB to ECS tasks on backend container port"
  from_port                = var.backend_target_port
  to_port                  = var.backend_target_port
  protocol                 = "tcp"
  security_group_id        = aws_security_group.alb.id
  source_security_group_id = var.ecs_security_group_id
}

###############################################################################
# ECS SG Ingress Rule from ALB (placed here to break SG circular dependency)
#
# This rule lives on the ECS tasks security group (var.ecs_security_group_id)
# and allows ingress FROM the ALB security group (aws_security_group.alb.id)
# on the backend container port.
#
# Per the folder-spec critical-requirement note: "placed here to break
# circular dependencies; alternative is to place it in modules/ecs/".
# Owning this rule in the ALB module avoids the module-level cycle that
# would arise if ecs took alb_security_group_id as input AND alb took
# ecs_security_group_id as input.
#
# IMPORTANT: The ECS module MUST NOT independently declare its own
# tasks_ingress_from_alb rule. If both modules try to create this rule,
# AWS will reject the duplicate.
###############################################################################

resource "aws_security_group_rule" "ecs_ingress_from_alb" {
  type                     = "ingress"
  description              = "ECS tasks ingress from ALB SG on backend container port (rule lives in ALB module to break SG circular dependency)"
  from_port                = var.backend_target_port
  to_port                  = var.backend_target_port
  protocol                 = "tcp"
  security_group_id        = var.ecs_security_group_id
  source_security_group_id = aws_security_group.alb.id
}

###############################################################################
# Application Load Balancer (Internet-facing)
#
# Internet-facing ALB across var.public_subnet_ids. Drops invalid headers
# as a defense against header smuggling. HTTP/2 enabled for SPA performance.
# Access logs delivered to the bucket from modules/observability/ (when enabled).
#
# enable_deletion_protection should be TRUE for production (prevents
# accidental tear-down), and FALSE for dev/staging to allow rapid teardown.
# Operators flip this via var.enable_deletion_protection per environment.
#
# ALBs always have cross-zone load balancing enabled (it cannot be turned
# off for ALB), but setting it explicitly to true documents the intent
# and prevents future drift if AWS ever changes defaults.
###############################################################################

resource "aws_lb" "main" {
  name               = local.alb_name
  internal           = false
  load_balancer_type = "application"
  security_groups    = [aws_security_group.alb.id]
  subnets            = var.public_subnet_ids

  idle_timeout                     = var.idle_timeout
  drop_invalid_header_fields       = var.drop_invalid_header_fields
  enable_http2                     = var.enable_http2
  enable_deletion_protection       = var.enable_deletion_protection
  enable_cross_zone_load_balancing = true

  dynamic "access_logs" {
    for_each = local.access_logs_active ? [1] : []
    content {
      bucket  = var.access_logs_bucket
      prefix  = "alb/${var.environment}"
      enabled = true
    }
  }

  tags = merge(local.module_tags, {
    Name = local.alb_name
  })
}

###############################################################################
# Backend Target Group (IP mode for Fargate)
#
# CRITICAL: target_type = "ip" is MANDATORY for Fargate. With target_type
# = "instance", ECS Fargate task ENIs cannot be registered (Fargate has no
# instance ID). This is documented in the folder spec critical-constraints.
#
# Health check hits var.backend_health_check_path (default /healthz) which
# is the unconditional liveness probe per AAP Sec 0.5.2. The /readyz path
# (DB ping) is intentionally NOT used here so the target group doesn't
# remove healthy instances on transient DB hiccups.
#
# deregistration_delay = 30 means draining tasks have 30s to complete
# in-flight requests before the ALB stops routing to them. This aligns
# with the ECS service stopTimeout = 30 in modules/ecs/locals.tf and with
# Gunicorn's graceful-shutdown window.
#
# create_before_destroy on the target group lets us replace it without
# downtime: the new TG is created and registered before the old one is
# deregistered.
###############################################################################

resource "aws_lb_target_group" "backend" {
  name                 = local.target_group_name
  port                 = var.backend_target_port
  protocol             = "HTTP"
  target_type          = "ip"
  vpc_id               = var.vpc_id
  deregistration_delay = 30

  health_check {
    enabled             = true
    path                = var.backend_health_check_path
    matcher             = var.backend_health_check_matcher
    interval            = var.backend_health_check_interval
    timeout             = var.backend_health_check_timeout
    healthy_threshold   = var.backend_health_check_healthy_threshold
    unhealthy_threshold = var.backend_health_check_unhealthy_threshold
    protocol            = "HTTP"
    port                = "traffic-port"
  }

  # Stickiness disabled - the application is stateless (per AAP Sec 0.7.1
  # architectural invariant: "Stateless backend workers"). Sticky sessions
  # would couple a client to a specific task and undermine horizontal scaling.
  stickiness {
    type    = "lb_cookie"
    enabled = false
  }

  tags = merge(local.module_tags, {
    Name = local.target_group_name
  })

  lifecycle {
    create_before_destroy = true
  }
}

###############################################################################
# HTTPS:443 Listener (TLS termination via ACM)
#
# CRITICAL: ssl_policy defaults to ELBSecurityPolicy-TLS13-1-2-2021-06,
# which supports TLS 1.3 with TLS 1.2 fallback. This satisfies the AAP
# Sec 0.7.4 invariant: "TLS terminates at the ALB with ACM-issued
# certificates. No internal cleartext traffic outside the VPC."
#
# certificate_arn is sourced from local.effective_certificate_arn which
# resolves to either var.acm_certificate_arn (when supplied) or the
# aws_acm_certificate.main[0].arn (when this module provisions a fresh cert).
#
# Default action forwards all traffic to the backend target group. Future
# path-based routing (e.g., /api/v2 -> different target group) can be
# added via aws_lb_listener_rule resources without modifying this default.
###############################################################################

resource "aws_lb_listener" "https" {
  load_balancer_arn = aws_lb.main.arn
  port              = 443
  protocol          = "HTTPS"
  ssl_policy        = var.ssl_policy
  certificate_arn   = local.effective_certificate_arn

  default_action {
    type             = "forward"
    target_group_arn = aws_lb_target_group.backend.arn
  }

  tags = merge(local.module_tags, {
    Name = "${var.name_prefix}-https-listener"
  })

  # The HTTPS listener depends on the ACM certificate being validated when
  # the cert is freshly provisioned. The lifecycle precondition catches
  # misconfigured states (no ARN supplied AND no domain to provision from).
  lifecycle {
    precondition {
      condition     = local.effective_certificate_arn != ""
      error_message = "HTTPS listener requires a certificate. Either supply var.acm_certificate_arn (existing cert) OR set var.domain_name (this module will provision via ACM). Both are empty."
    }
  }
}

###############################################################################
# HTTP:80 Listener (Redirect to HTTPS:443)
#
# CRITICAL: HTTP:80 ONLY redirects to HTTPS:443 with HTTP_301 (per AAP
# Sec 0.7.4: "TLS terminates at the ALB"). No plaintext data path is
# permitted. Adding any forwarding rule here would violate the HTTPS-only
# invariant.
#
# The redirect uses host = "#{host}", path = "/#{path}", query = "#{query}"
# implicitly (these are the AWS defaults when omitted), preserving the
# original request URL on redirect.
###############################################################################

resource "aws_lb_listener" "http" {
  load_balancer_arn = aws_lb.main.arn
  port              = 80
  protocol          = "HTTP"

  default_action {
    type = "redirect"

    redirect {
      port        = "443"
      protocol    = "HTTPS"
      status_code = "HTTP_301"
    }
  }

  tags = merge(local.module_tags, {
    Name = "${var.name_prefix}-http-listener"
  })
}
