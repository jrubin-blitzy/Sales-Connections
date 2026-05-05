###############################################################################
# infra/terraform/modules/network/variables.tf
#
# Input variables for the Sales-Connections network module.
#
# Variable groups (in declaration order below):
#   1. Identity / naming           : name_prefix, environment
#   2. VPC and CIDR sizing         : vpc_cidr
#   3. Availability zones          : availability_zones
#   4. Subnet CIDRs                : public_subnet_cidrs, private_subnet_cidrs
#   5. NAT gateway tuning          : enable_nat_gateway, single_nat_gateway
#   6. Optional VPC endpoints      : create_vpc_endpoints
#   7. Tagging                     : tags
#
# Validation rules:
#   - vpc_cidr must be a valid CIDR block
#   - availability_zones must be non-empty and <= 6 entries
#   - subnet CIDR list lengths must match availability_zones length
#     (enforced via lifecycle.precondition on aws_vpc.main since
#     cross-variable validation requires Terraform 1.9+; this module
#     supports 1.7+ per AAP Sec 0.3.1)
###############################################################################

###############################################################################
# Identity and naming
###############################################################################

variable "name_prefix" {
  description = "Resource name prefix used in tags and Name labels (e.g., 'sales-connections-prod'). Composed by the parent as '$${var.project}-$${var.environment}'."
  type        = string

  validation {
    condition     = length(var.name_prefix) > 0 && length(var.name_prefix) <= 40
    error_message = "name_prefix must be a non-empty string of at most 40 characters (AWS resource name limits leave room for suffixes like '-vpc', '-igw')."
  }

  validation {
    condition     = can(regex("^[a-z][a-z0-9-]*[a-z0-9]$", var.name_prefix))
    error_message = "name_prefix must start with a lowercase letter, end with a letter or digit, and contain only lowercase letters, digits, and hyphens."
  }
}

variable "environment" {
  description = "Environment label (one of: dev, staging, prod). Used in Name tags and Environment tag for cost-explorer drill-down."
  type        = string

  validation {
    condition     = contains(["dev", "staging", "prod"], var.environment)
    error_message = "environment must be one of: dev, staging, prod."
  }
}

###############################################################################
# VPC CIDR
###############################################################################

variable "vpc_cidr" {
  description = "Primary CIDR block for the VPC (e.g., '10.10.0.0/16'). Each environment uses a non-overlapping range to keep VPC peering an option later."
  type        = string
  default     = "10.10.0.0/16"

  validation {
    condition     = can(cidrhost(var.vpc_cidr, 0))
    error_message = "vpc_cidr must be a valid CIDR block (e.g., '10.10.0.0/16')."
  }

  # Wrapped with try(..., true) so that a malformed CIDR (caught by the
  # previous validation rule) does not raise a confusing "Invalid index"
  # error from split("/", ...)[1] on Terraform 1.7+. When the CIDR is
  # malformed, this rule passes vacuously and the first rule surfaces the
  # canonical error message.
  validation {
    condition     = try(tonumber(split("/", var.vpc_cidr)[1]) <= 28, true)
    error_message = "vpc_cidr prefix length must be /28 or larger (smaller number) to fit subnets. Recommended: /16 for production-scale capacity."
  }
}

###############################################################################
# Availability zones
###############################################################################

variable "availability_zones" {
  description = "List of availability zones the network spans. Production should use at least 2 AZs for Multi-AZ; dev may use 1 to save NAT/EIP costs. Order in this list is preserved across all subnet/route-table/NAT outputs (subnet[i] -> AZ[i])."
  type        = list(string)

  validation {
    condition     = length(var.availability_zones) >= 1
    error_message = "availability_zones must contain at least one zone."
  }

  validation {
    condition     = length(var.availability_zones) <= 6
    error_message = "availability_zones must contain at most six zones (AWS regional limit)."
  }

  validation {
    condition     = length(var.availability_zones) == length(distinct(var.availability_zones))
    error_message = "availability_zones must not contain duplicate entries."
  }

  validation {
    condition     = alltrue([for az in var.availability_zones : can(regex("^[a-z]{2}-[a-z]+-[0-9]+[a-z]$", az))])
    error_message = "Each availability_zone must be a valid AWS AZ identifier (e.g., 'us-east-1a')."
  }
}

###############################################################################
# Subnet CIDRs
#
# Index alignment: public_subnet_cidrs[i] and private_subnet_cidrs[i] are
# placed in availability_zones[i]. The list lengths must match
# length(var.availability_zones); this is enforced via lifecycle.precondition
# on aws_vpc.main (cross-variable validation requires Terraform 1.9+).
###############################################################################

variable "public_subnet_cidrs" {
  description = "CIDR blocks for the public subnets. One per AZ (must equal length of var.availability_zones). Each must be a subset of var.vpc_cidr. Default sizing: /24 per subnet (~250 hosts) suffices for the MVP 10K-record ceiling and accommodates ALB ENIs."
  type        = list(string)
  default     = ["10.10.0.0/24", "10.10.1.0/24", "10.10.2.0/24"]

  validation {
    condition     = length(var.public_subnet_cidrs) > 0
    error_message = "public_subnet_cidrs must not be empty."
  }

  validation {
    condition     = alltrue([for cidr in var.public_subnet_cidrs : can(cidrhost(cidr, 0))])
    error_message = "Each entry in public_subnet_cidrs must be a valid CIDR block."
  }

  validation {
    condition     = length(var.public_subnet_cidrs) == length(distinct(var.public_subnet_cidrs))
    error_message = "public_subnet_cidrs must not contain duplicate entries."
  }
}

variable "private_subnet_cidrs" {
  description = "CIDR blocks for the private subnets. One per AZ (must equal length of var.availability_zones). Each must be a subset of var.vpc_cidr and must NOT overlap with var.public_subnet_cidrs. Default sizing: /24 per subnet (~250 hosts) suffices for ECS task density at MVP scale."
  type        = list(string)
  default     = ["10.10.10.0/24", "10.10.11.0/24", "10.10.12.0/24"]

  validation {
    condition     = length(var.private_subnet_cidrs) > 0
    error_message = "private_subnet_cidrs must not be empty."
  }

  validation {
    condition     = alltrue([for cidr in var.private_subnet_cidrs : can(cidrhost(cidr, 0))])
    error_message = "Each entry in private_subnet_cidrs must be a valid CIDR block."
  }

  validation {
    condition     = length(var.private_subnet_cidrs) == length(distinct(var.private_subnet_cidrs))
    error_message = "private_subnet_cidrs must not contain duplicate entries."
  }
}

###############################################################################
# NAT gateway tuning
###############################################################################

variable "enable_nat_gateway" {
  description = "Whether to provision NAT gateways so private subnets can reach the Internet. Required for ECS tasks to call Anthropic Claude and Google OAuth APIs. Set true for all environments in MVP; false only for fully air-gapped subnet experiments."
  type        = bool
  default     = true
}

variable "single_nat_gateway" {
  description = "If true, a single NAT gateway is shared across all AZs (cost-optimized; acceptable for dev). If false, one NAT gateway per AZ (HA-optimized; required for prod per AAP Sec 0.4.6 'Multi-AZ' guidance). Only relevant when var.enable_nat_gateway is true."
  type        = bool
  default     = false
}

###############################################################################
# Optional VPC endpoints (cost optimization for production)
###############################################################################

variable "create_vpc_endpoints" {
  description = "If true, creates VPC endpoints for S3 (gateway), ECR API, ECR Docker registry, Secrets Manager, CloudWatch Logs, and KMS (interface). This reduces NAT data-transfer costs and improves reliability for ECS image pulls and Secrets Manager reads. Default false for MVP simplicity (per folder spec); production composition typically sets true. Endpoint resources live in vpc_endpoints.tf."
  type        = bool
  default     = false
}

###############################################################################
# Tagging
###############################################################################

variable "tags" {
  description = "Map of tags merged into every resource created by this module. The module layers in 'Component = \"Network\"' and 'Environment = var.environment' on top of these. The parent composition's provider-level default_tags also apply."
  type        = map(string)
  default     = {}
}
