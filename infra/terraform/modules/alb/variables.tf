###############################################################################
# infra/terraform/modules/alb/variables.tf
#
# Input variables for the Sales-Connections Application Load Balancer (ALB)
# module. This file is the most foundational file in the module - every
# other file (main.tf, acm.tf, locals.tf, outputs.tf) references variables
# declared here.
#
# Variable groups (in declaration order below):
#   1. Identity                       : name_prefix, environment, tags
#   2. Network plumbing               : vpc_id, public_subnet_ids,
#                                       ecs_security_group_id
#   3. Backend target group           : backend_target_port,
#                                       backend_health_check_path,
#                                       backend_health_check_interval,
#                                       backend_health_check_timeout,
#                                       backend_health_check_healthy_threshold,
#                                       backend_health_check_unhealthy_threshold,
#                                       backend_health_check_matcher
#   4. ALB tuning                     : idle_timeout,
#                                       drop_invalid_header_fields,
#                                       enable_http2,
#                                       enable_deletion_protection
#   5. Public access policy           : allowed_ingress_cidrs
#   6. TLS configuration              : ssl_policy, acm_certificate_arn,
#                                       domain_name, alternative_domain_names,
#                                       wait_for_certificate_validation
#   7. Access logs                    : access_logs_enabled, access_logs_bucket
#
# Validation strategy:
#   - ARN-formatted variables validated via regex; the partition is matched
#     as [a-z0-9-]+ to support standard AWS ("aws"), GovCloud ("aws-us-gov"),
#     and China ("aws-cn") partitions without modification.
#   - Enum-valued variables validated via contains([...], var.X).
#   - Numeric ranges validated via comparison.
#   - All validation runs at plan time so misconfiguration is caught before
#     apply (fail-fast feedback loop).
#
# Default-value policy (per folder spec critical constraints from AAP):
#   - drop_invalid_header_fields = true (security invariant - header smuggling)
#   - enable_http2               = true (SPA performance + modern client compat)
#   - ssl_policy                 = ELBSecurityPolicy-TLS13-1-2-2021-06
#                                  (modern TLS 1.3 with TLS 1.2 fallback)
#   - backend_target_port        = 8000 (Gunicorn convention in Dockerfile)
#   - backend_health_check_path  = /healthz (unconditional liveness, NOT
#                                  /readyz which includes a DB ping that
#                                  would cause flapping on transient hiccups)
#   - idle_timeout               = 60   (covers 5-second AI budget per AAP
#                                  Sec 0.7.3 plus comfortable margin)
#   - allowed_ingress_cidrs      = ["0.0.0.0/0"] (full public Internet)
#
# Cross-variable validation note:
#   Terraform 1.7 (the project's pinned floor per versions.tf) does not allow
#   cross-variable references inside a variable's validation block. Pairs that
#   require cross-checking (e.g., backend_health_check_timeout must be less
#   than backend_health_check_interval, acm_certificate_arn empty when
#   domain_name empty) are enforced via lifecycle.precondition on the
#   relevant resources in main.tf or by the AWS API at apply time.
###############################################################################

###############################################################################
# 1. Identity
###############################################################################

variable "name_prefix" {
  description = "Naming prefix applied to all ALB resources (load balancer, target group, security group, ACM cert). Should include the project and environment, e.g., 'sales-connections-prod'. Combined with resource-specific suffixes (e.g., '-alb', '-backend-tg', '-alb-sg') to form final AWS resource names. The 28-character cap leaves room for the longest suffix '-alb' (4 chars) within the AWS load balancer name limit of 32 chars."
  type        = string

  validation {
    condition     = length(var.name_prefix) > 0 && length(var.name_prefix) <= 28
    error_message = "name_prefix must be 1-28 characters (AWS load balancer names limited to 32 chars; 4 chars reserved for the longest suffix '-alb')."
  }

  validation {
    condition     = can(regex("^[a-z0-9][a-z0-9-]*[a-z0-9]$", var.name_prefix))
    error_message = "name_prefix must start and end with a lowercase alphanumeric and may contain hyphens (e.g., 'sales-connections-prod'). Underscores and uppercase are not permitted by AWS load balancer naming rules."
  }
}

variable "environment" {
  description = "Deployment environment label (one of: dev, staging, prod). Used in tag values, access logs S3 prefix, and consistency with sibling modules. Adding a new environment requires explicit operator action (update validation rule + decision-log entry per AAP Sec 0.7.5 Explainability rule)."
  type        = string

  validation {
    condition     = contains(["dev", "staging", "prod"], var.environment)
    error_message = "environment must be one of: dev, staging, prod."
  }
}

variable "tags" {
  description = "Map of additional tags merged into every taggable resource created by this module. The module layers in 'Component = ALB', 'Environment = var.environment', 'ManagedBy = Terraform', and 'Module = alb' tags automatically (in locals.tf) - those module-internal tags cannot be overridden via this variable. The parent composition's provider-level default_tags also apply via AWS provider 5.x default_tags."
  type        = map(string)
  default     = {}
}

###############################################################################
# 2. Network plumbing (consumed from modules/network/ and modules/ecs/)
###############################################################################

variable "vpc_id" {
  description = "VPC identifier where the ALB and its target group live. Must be the same VPC as the ECS tasks so the ALB can route traffic to task ENIs over private VPC links. Sourced from module.network.vpc_id in the root composition."
  type        = string

  validation {
    condition     = can(regex("^vpc-[0-9a-f]{8,17}$", var.vpc_id))
    error_message = "vpc_id must be a valid VPC ID matching 'vpc-XXXXXXXX' (8-17 hex characters), e.g., 'vpc-0abc123def456789a'."
  }
}

variable "public_subnet_ids" {
  description = "List of public subnet IDs across availability zones where the ALB places its ENIs. AWS requires the ALB to span at least 2 AZs for high availability. Sourced from module.network.public_subnet_ids in the root composition. Each subnet must have a route to the Internet Gateway so the ALB can serve public Internet traffic."
  type        = list(string)

  validation {
    condition     = length(var.public_subnet_ids) >= 2
    error_message = "public_subnet_ids must contain at least 2 subnets across distinct availability zones (AWS ALB requires Multi-AZ placement)."
  }

  validation {
    condition = alltrue([
      for s in var.public_subnet_ids : can(regex("^subnet-[0-9a-f]{8,17}$", s))
    ])
    error_message = "Every entry in public_subnet_ids must be a valid subnet ID matching 'subnet-XXXXXXXX' (8-17 hex characters)."
  }
}

variable "ecs_security_group_id" {
  description = "Security group ID of the ECS Fargate tasks. The ALB module creates: (a) an EGRESS rule on the ALB security group permitting traffic to this SG on var.backend_target_port, AND (b) an INGRESS rule on this SG permitting traffic from the ALB SG. Owning both rules in the ALB module breaks the SG circular dependency that would otherwise arise between modules/alb/ and modules/ecs/. Sourced from module.ecs.security_group_id in the root composition."
  type        = string

  validation {
    condition     = can(regex("^sg-[0-9a-f]{8,17}$", var.ecs_security_group_id))
    error_message = "ecs_security_group_id must be a valid security group ID matching 'sg-XXXXXXXX' (8-17 hex characters), e.g., 'sg-0abc123def456789a'."
  }
}

###############################################################################
# 3. Backend target group (port + health check)
###############################################################################

variable "backend_target_port" {
  description = "TCP port where the backend Gunicorn process listens inside the ECS Fargate task. The ALB target group registers ECS task ENIs on this port. Default 8000 matches the Gunicorn binding convention in backend/Dockerfile (gunicorn --bind 0.0.0.0:8000). Must match modules/ecs/ var.backend_container_port."
  type        = number
  default     = 8000

  validation {
    condition     = var.backend_target_port >= 1 && var.backend_target_port <= 65535
    error_message = "backend_target_port must be between 1 and 65535 (valid TCP port range)."
  }
}

variable "backend_health_check_path" {
  description = "HTTP path on the backend that the ALB target group health check probes. Default /healthz is the unconditional liveness probe per AAP Sec 0.5.2 (returns 200 unconditionally). DO NOT use /readyz here because that probe includes a DB ping and would cause flapping on transient DB hiccups; the readiness probe belongs in container-level health checks, NOT ALB-level."
  type        = string
  default     = "/healthz"

  validation {
    condition     = can(regex("^/", var.backend_health_check_path))
    error_message = "backend_health_check_path must start with '/' (e.g., '/healthz')."
  }
}

variable "backend_health_check_interval" {
  description = "Seconds between successive health check probes. Default 30. Range 5-300 per AWS limits. Lower values detect failures faster but increase target load and probe traffic."
  type        = number
  default     = 30

  validation {
    condition     = var.backend_health_check_interval >= 5 && var.backend_health_check_interval <= 300
    error_message = "backend_health_check_interval must be between 5 and 300 seconds (AWS API limits)."
  }
}

variable "backend_health_check_timeout" {
  description = "Seconds the ALB waits for a response on each health check probe. Default 5. Must be strictly less than backend_health_check_interval; this cross-variable check is enforced at apply time by the AWS API. Range 2-120 per AWS limits."
  type        = number
  default     = 5

  validation {
    condition     = var.backend_health_check_timeout >= 2 && var.backend_health_check_timeout <= 120
    error_message = "backend_health_check_timeout must be between 2 and 120 seconds (AWS API limits) and must be strictly less than backend_health_check_interval."
  }
}

variable "backend_health_check_healthy_threshold" {
  description = "Consecutive successful probes required to mark a target HEALTHY (and start routing traffic to it). Default 2 means the target becomes healthy after 2 consecutive 200 responses. Range 2-10 per AWS limits."
  type        = number
  default     = 2

  validation {
    condition     = var.backend_health_check_healthy_threshold >= 2 && var.backend_health_check_healthy_threshold <= 10
    error_message = "backend_health_check_healthy_threshold must be between 2 and 10 (AWS API limits)."
  }
}

variable "backend_health_check_unhealthy_threshold" {
  description = "Consecutive failed probes required to mark a target UNHEALTHY (and stop routing traffic to it). Default 3 means the target is taken out of rotation after 3 consecutive failures, which combined with the 30-second interval gives a ~90-second detection window. Range 2-10 per AWS limits."
  type        = number
  default     = 3

  validation {
    condition     = var.backend_health_check_unhealthy_threshold >= 2 && var.backend_health_check_unhealthy_threshold <= 10
    error_message = "backend_health_check_unhealthy_threshold must be between 2 and 10 (AWS API limits)."
  }
}

variable "backend_health_check_matcher" {
  description = "HTTP status code(s) the ALB accepts as healthy on probe responses. Default '200'. Comma-separated lists and ranges are supported (e.g., '200,301-302' or '200-299'). For /healthz, only '200' is expected - non-200 indicates a real failure that should mark the target unhealthy."
  type        = string
  default     = "200"

  validation {
    condition     = length(var.backend_health_check_matcher) > 0
    error_message = "backend_health_check_matcher must be non-empty (e.g., '200' or '200-299')."
  }
}

###############################################################################
# 4. ALB tuning
###############################################################################

variable "idle_timeout" {
  description = "Seconds a connection may remain idle before the ALB closes it. Default 60 covers the 5-second AI budget per AAP Sec 0.7.3 plus comfortable margin for slow client connections. Range 1-4000 per AWS limits."
  type        = number
  default     = 60

  validation {
    condition     = var.idle_timeout >= 1 && var.idle_timeout <= 4000
    error_message = "idle_timeout must be between 1 and 4000 seconds (AWS API limits)."
  }
}

variable "drop_invalid_header_fields" {
  description = "Whether the ALB drops malformed HTTP headers before forwarding to targets. Default true (REQUIRED per folder spec critical constraint as defense against HTTP header smuggling attacks). NEVER override to false in production - downstream applications expect well-formed headers and rely on the ALB to reject malformed ones."
  type        = bool
  default     = true
}

variable "enable_http2" {
  description = "Whether the ALB negotiates HTTP/2 with clients. Default true (REQUIRED per folder spec critical constraint for SPA performance and modern client compatibility). HTTP/2 multiplexes multiple requests over a single TCP connection, reducing latency for the React SPA's many parallel API calls."
  type        = bool
  default     = true
}

variable "enable_deletion_protection" {
  description = "Whether AWS prevents Terraform from destroying the ALB. Default false (allows tear-down for dev/staging environments). Production composition should set this to true to prevent accidental destruction. Operators must explicitly disable this before issuing 'terraform destroy' against a protected ALB in production."
  type        = bool
  default     = false
}

###############################################################################
# 5. Public access policy
###############################################################################

variable "allowed_ingress_cidrs" {
  description = "List of CIDR blocks permitted to reach the ALB on ports 80 (HTTP redirect) and 443 (HTTPS). Default ['0.0.0.0/0'] (full public Internet) is the typical public-facing exposure. Operators may tighten this for environments behind a corporate WAF or VPN (e.g., dev/staging restricted to office IP ranges). The HTTP listener uniformly redirects to HTTPS regardless of source CIDR."
  type        = list(string)
  default     = ["0.0.0.0/0"]

  validation {
    condition     = length(var.allowed_ingress_cidrs) >= 1
    error_message = "allowed_ingress_cidrs must contain at least one CIDR block."
  }

  validation {
    condition = alltrue([
      for cidr in var.allowed_ingress_cidrs : can(cidrhost(cidr, 0))
    ])
    error_message = "Each entry in allowed_ingress_cidrs must be a valid CIDR block (e.g., '10.0.0.0/8' or '0.0.0.0/0')."
  }
}

###############################################################################
# 6. TLS configuration (ACM certificate + SSL policy)
###############################################################################

variable "ssl_policy" {
  description = "AWS-managed SSL/TLS policy for the HTTPS listener. Default 'ELBSecurityPolicy-TLS13-1-2-2021-06' supports TLS 1.3 with TLS 1.2 fallback - the canonical modern policy. NEVER use older policies that accept TLS 1.0/1.1 (e.g., 'ELBSecurityPolicy-2016-08') - they are insecure and violate the AAP Sec 0.7.4 'TLS terminates at the ALB with ACM-issued certificates' invariant. See AWS docs for the current list of valid policies."
  type        = string
  default     = "ELBSecurityPolicy-TLS13-1-2-2021-06"

  validation {
    condition     = can(regex("^ELBSecurityPolicy-", var.ssl_policy))
    error_message = "ssl_policy must be a valid AWS ELB security policy name (starts with 'ELBSecurityPolicy-')."
  }
}

variable "acm_certificate_arn" {
  description = "ARN of an existing ACM certificate to attach to the HTTPS listener. When non-empty, the ALB uses this certificate directly. When empty AND var.domain_name is non-empty, the module provisions a new certificate via ACM (DNS validation). When BOTH are empty, the HTTPS listener creation will fail at plan time via lifecycle.precondition in main.tf. Supply this ARN to bypass certificate provisioning in this module (e.g., when the certificate is managed in a different account or by a sibling module)."
  type        = string
  default     = ""

  validation {
    condition     = var.acm_certificate_arn == "" || can(regex("^arn:[a-z0-9-]+:acm:[a-z0-9-]+:[0-9]+:certificate/", var.acm_certificate_arn))
    error_message = "acm_certificate_arn must be empty (triggering provisioning) OR a valid ACM certificate ARN matching 'arn:<partition>:acm:<region>:<account>:certificate/...'."
  }
}

variable "domain_name" {
  description = "Primary domain name for the ACM certificate (e.g., 'app.sales-connections.example.com'). Required when acm_certificate_arn is empty (the module will provision a certificate with this domain as the CommonName). Empty string disables domain-based provisioning; in that case acm_certificate_arn MUST be supplied or the HTTPS listener creation will fail at plan time. This module does NOT create Route53 records - DNS validation records must be created out-of-band or via a sibling Route53 module."
  type        = string
  default     = ""

  validation {
    condition     = var.domain_name == "" || can(regex("^[a-z0-9][a-z0-9.-]*\\.[a-z]{2,}$", var.domain_name))
    error_message = "domain_name must be empty OR a valid lowercase domain name (e.g., 'app.example.com'). Use lowercase only - DNS is case-insensitive but ACM canonicalizes to lowercase."
  }
}

variable "alternative_domain_names" {
  description = "Subject Alternative Names (SANs) attached to the ACM certificate. Useful for serving multiple hostnames (e.g., the apex 'example.com' plus 'www.example.com') under one certificate. Empty list when only domain_name is needed. Has no effect when acm_certificate_arn is supplied (the existing certificate's SANs are used as-is). Wildcards like '*.example.com' are permitted (ACM supports wildcard SANs)."
  type        = list(string)
  default     = []

  validation {
    condition = alltrue([
      for d in var.alternative_domain_names : can(regex("^[a-z0-9*][a-z0-9.*-]*\\.[a-z]{2,}$", d))
    ])
    error_message = "Each alternative_domain_name must be a valid lowercase domain (wildcards like '*.example.com' are permitted)."
  }
}

variable "wait_for_certificate_validation" {
  description = "When true, terraform apply BLOCKS until the freshly provisioned ACM certificate is ISSUED (i.e., DNS validation has completed). Requires DNS validation records to be created out-of-band or via a sibling Route53 module. Default false avoids the apply-hang scenario when DNS records are not yet in place; operators handle a two-step apply (provision cert -> create DNS records -> re-apply) instead. Has no effect when acm_certificate_arn is supplied (no provisioning happens)."
  type        = bool
  default     = false
}

###############################################################################
# 7. Access logs
###############################################################################

variable "access_logs_enabled" {
  description = "Whether ALB access logs are written to S3. Default true honors the AAP Sec 0.7.5 Observability rule. When true AND var.access_logs_bucket is non-empty, the ALB delivers logs to the bucket. When the bucket name is empty (e.g., during initial bootstrap before modules/observability/ provisions it), this flag is honored but the dynamic access_logs block in main.tf is skipped silently - no error."
  type        = bool
  default     = true
}

variable "access_logs_bucket" {
  description = "Name of the S3 bucket receiving ALB access logs. Sourced from module.observability.access_logs_bucket_name in the root composition. Default empty allows the ALB module to be applied BEFORE modules/observability/ exists; in that case access logs are silently skipped (the ALB still functions normally). The bucket must have a policy permitting the AWS-managed elasticloadbalancing principal to write logs - this is provisioned by modules/observability/ when this module's bucket name is supplied."
  type        = string
  default     = ""
}
