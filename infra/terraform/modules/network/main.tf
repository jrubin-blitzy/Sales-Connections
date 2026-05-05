###############################################################################
# infra/terraform/modules/network/main.tf
#
# Foundational AWS network resources for the Sales-Connections platform:
#
#   - VPC (10.10.0.0/16 default; per-environment override)
#   - Public subnets, one per availability zone
#   - Private subnets, one per availability zone
#   - Internet Gateway
#   - NAT Gateways with Elastic IPs (one per AZ for HA, or one shared for cost)
#   - Public route table (shared) routing 0.0.0.0/0 to the IGW
#   - Private route tables (one per AZ for HA, or one shared) routing
#     0.0.0.0/0 to the AZ-local NAT gateway
#   - Locked-down default security group (zero ingress/egress rules)
#
# This module is the FIRST in the AAP Sec 0.5.2 build order; every other
# module (database, ecs, ecr, secrets, alb, observability) consumes its
# outputs. Optional VPC endpoints live in the sibling vpc_endpoints.tf file.
#
# Resource graph dependency chain (within this module):
#
#     aws_vpc.main
#       |-> aws_default_security_group.lockdown
#       |-> aws_internet_gateway.main
#       |-> aws_subnet.public[*]
#       |-> aws_subnet.private[*]
#
#     aws_internet_gateway.main
#       |-> aws_eip.nat[*]
#             |-> aws_nat_gateway.main[*]
#                   |-> aws_route.private_nat[*]
#       |-> aws_route.public_internet
#
#     aws_route_table.public
#       |-> aws_route.public_internet
#       |-> aws_route_table_association.public[*]
#
#     aws_route_table.private[*]
#       |-> aws_route.private_nat[*]
#       |-> aws_route_table_association.private[*]
#
# Inputs consumed (from variables.tf):
#   - var.name_prefix          : Name-tag prefix (e.g., "sales-connections-prod")
#   - var.vpc_cidr             : VPC primary CIDR block
#   - var.availability_zones   : Ordered AZ list; subnet[i] -> AZ[i]
#   - var.public_subnet_cidrs  : Public subnet CIDRs (length must match AZs)
#   - var.private_subnet_cidrs : Private subnet CIDRs (length must match AZs)
#   - var.single_nat_gateway   : Single shared NAT vs per-AZ NAT toggle
#
# Locals consumed (from locals.tf):
#   - local.module_tags               : Base tag set merged into every resource
#   - local.az_count                  : length(var.availability_zones)
#   - local.nat_gateway_count         : 0 / 1 / az_count depending on tunables
#   - local.private_route_table_count : 1 / az_count depending on tunables
###############################################################################

###############################################################################
# VPC
#
# enable_dns_hostnames AND enable_dns_support are BOTH required for
# AWS-managed DNS resolution within the VPC. This is critical for:
#   - RDS endpoint hostname resolution from ECS tasks
#   - VPC interface-endpoint private DNS overrides (Secrets Manager,
#     ECR API, CloudWatch Logs)
#   - Route 53 private hosted zones (future)
#
# Pre-conditions are attached here (rather than in a terraform_data
# resource) so plan-time validation runs before any AWS API call. They
# enforce the index-alignment invariant that subnet[i] lives in AZ[i].
###############################################################################

resource "aws_vpc" "main" {
  cidr_block           = var.vpc_cidr
  enable_dns_hostnames = true
  enable_dns_support   = true
  instance_tenancy     = "default"

  tags = merge(local.module_tags, {
    Name = "${var.name_prefix}-vpc"
  })

  lifecycle {
    precondition {
      condition     = length(var.public_subnet_cidrs) == length(var.availability_zones)
      error_message = "var.public_subnet_cidrs (${length(var.public_subnet_cidrs)} entries) must have the same length as var.availability_zones (${length(var.availability_zones)} entries)."
    }

    precondition {
      condition     = length(var.private_subnet_cidrs) == length(var.availability_zones)
      error_message = "var.private_subnet_cidrs (${length(var.private_subnet_cidrs)} entries) must have the same length as var.availability_zones (${length(var.availability_zones)} entries)."
    }

    precondition {
      condition     = length(var.availability_zones) >= 1
      error_message = "var.availability_zones must contain at least one zone."
    }
  }
}

###############################################################################
# Default security group lockdown
#
# AWS creates a default SG on every VPC with permissive rules (allow all
# from same SG, allow all egress). We claim it with Terraform and remove
# all ingress/egress (an aws_default_security_group resource with no rule
# blocks). Resources that inadvertently attach to the default SG inherit
# zero connectivity (defense-in-depth).
###############################################################################

resource "aws_default_security_group" "lockdown" {
  vpc_id = aws_vpc.main.id

  # Intentionally NO ingress or egress blocks. This removes all default
  # rules from the default SG.

  tags = merge(local.module_tags, {
    Name = "${var.name_prefix}-default-sg-locked"
  })
}

###############################################################################
# Internet Gateway
#
# Required for public-subnet egress to the Internet AND as the underlying
# anchor for NAT-gateway Elastic IPs. Exactly one IGW per VPC.
###############################################################################

resource "aws_internet_gateway" "main" {
  vpc_id = aws_vpc.main.id

  tags = merge(local.module_tags, {
    Name = "${var.name_prefix}-igw"
  })
}

###############################################################################
# Public subnets (one per availability zone)
#
# Defense-in-depth: map_public_ip_on_launch = false prevents auto-assignment
# of public IPs to launched ENIs. Public exposure is ALB-managed; nothing
# else in these subnets should be Internet-reachable.
#
# Naming uses the actual AZ identifier (e.g., us-east-1a) rather than the
# index, so operators can identify subnets by AZ in the AWS console
# without a lookup.
###############################################################################

resource "aws_subnet" "public" {
  count = local.az_count

  vpc_id                  = aws_vpc.main.id
  cidr_block              = var.public_subnet_cidrs[count.index]
  availability_zone       = var.availability_zones[count.index]
  map_public_ip_on_launch = false

  tags = merge(local.module_tags, {
    Name = "${var.name_prefix}-public-${var.availability_zones[count.index]}"
    Tier = "Public"
  })
}

###############################################################################
# Private subnets (one per availability zone)
#
# Hosts ECS Fargate tasks, RDS PostgreSQL, and future internal workers.
# Egress to the Internet flows through the NAT gateway(s) declared below.
# RDS Multi-AZ requires at least two private subnets across two AZs.
###############################################################################

resource "aws_subnet" "private" {
  count = local.az_count

  vpc_id            = aws_vpc.main.id
  cidr_block        = var.private_subnet_cidrs[count.index]
  availability_zone = var.availability_zones[count.index]

  tags = merge(local.module_tags, {
    Name = "${var.name_prefix}-private-${var.availability_zones[count.index]}"
    Tier = "Private"
  })
}

###############################################################################
# Elastic IPs for NAT Gateways
#
# Count: local.nat_gateway_count
#   - 0 when var.enable_nat_gateway is false
#   - 1 when var.single_nat_gateway is true (cost-optimized, single AZ NAT)
#   - length(var.availability_zones) otherwise (HA-optimized, per-AZ NAT)
#
# domain = "vpc" associates the EIP with the VPC scope (the only valid
# scope post EC2-Classic deprecation; explicit for future-proofing).
###############################################################################

resource "aws_eip" "nat" {
  count = local.nat_gateway_count

  domain = "vpc"

  tags = merge(local.module_tags, {
    Name = "${var.name_prefix}-nat-eip-${count.index}"
  })

  # The IGW must exist before EIPs can be allocated for NAT use. The
  # explicit depends_on documents the dependency for human readers; the
  # resource graph already orders this correctly via the indirect
  # reference chain (NAT gateway -> EIP -> Internet Gateway).
  depends_on = [aws_internet_gateway.main]
}

###############################################################################
# NAT Gateways
#
# When var.single_nat_gateway is true, exactly one NAT gateway is created
# in the first public subnet (cost-optimized for dev). Otherwise, one NAT
# gateway is created per AZ in the corresponding public subnet (HA for
# prod). NAT gateways are AZ-local: an AZ outage takes its NAT with it,
# which is why per-AZ deployment is preferred for production.
###############################################################################

resource "aws_nat_gateway" "main" {
  count = local.nat_gateway_count

  allocation_id = aws_eip.nat[count.index].id

  # Subnet placement:
  #   single_nat_gateway = true  -> all NAT in the first public subnet
  #   single_nat_gateway = false -> one NAT per public subnet (per AZ)
  subnet_id = aws_subnet.public[var.single_nat_gateway ? 0 : count.index].id

  tags = merge(local.module_tags, {
    Name = "${var.name_prefix}-nat-${count.index}"
  })

  # NAT gateways require a functional Internet Gateway in the VPC; the
  # AWS API will reject creation otherwise. The explicit depends_on
  # documents this requirement; the resource graph orders correctly via
  # the EIP dependency anyway.
  depends_on = [aws_internet_gateway.main]
}

###############################################################################
# Public route table
#
# Single shared route table. All public subnets associate to this table,
# which routes 0.0.0.0/0 to the Internet Gateway. The implicit local
# route (var.vpc_cidr -> local) is added by AWS automatically and is
# not declared here.
###############################################################################

resource "aws_route_table" "public" {
  vpc_id = aws_vpc.main.id

  tags = merge(local.module_tags, {
    Name = "${var.name_prefix}-public-rt"
  })
}

resource "aws_route" "public_internet" {
  route_table_id         = aws_route_table.public.id
  destination_cidr_block = "0.0.0.0/0"
  gateway_id             = aws_internet_gateway.main.id
}

resource "aws_route_table_association" "public" {
  count = local.az_count

  subnet_id      = aws_subnet.public[count.index].id
  route_table_id = aws_route_table.public.id
}

###############################################################################
# Private route tables
#
# When var.single_nat_gateway is true OR var.enable_nat_gateway is false,
# one shared private route table is created. Otherwise, one route table
# per AZ is created so each AZ's private subnet routes through its own
# AZ-local NAT gateway (cross-AZ NAT traffic is undesirable both for
# latency and for AZ-failure blast radius).
#
# Naming is conditional: if there is exactly one private route table
# (single NAT or no NAT), the AZ suffix is dropped to avoid the
# misleading "rt-0" name; otherwise the AZ identifier is used.
#
# When NAT is disabled entirely, this route table still exists (count
# is at least 1) but has no 0.0.0.0/0 route - intra-VPC traffic continues
# to work via the implicit local route.
###############################################################################

resource "aws_route_table" "private" {
  count = local.private_route_table_count

  vpc_id = aws_vpc.main.id

  tags = merge(local.module_tags, {
    Name = local.private_route_table_count == 1 ? "${var.name_prefix}-private-rt" : "${var.name_prefix}-private-rt-${var.availability_zones[count.index]}"
  })
}

resource "aws_route" "private_nat" {
  count = local.nat_gateway_count

  route_table_id         = aws_route_table.private[count.index].id
  destination_cidr_block = "0.0.0.0/0"
  nat_gateway_id         = aws_nat_gateway.main[count.index].id
}

resource "aws_route_table_association" "private" {
  count = local.az_count

  subnet_id = aws_subnet.private[count.index].id

  # Route table mapping:
  #   single private RT (single_nat OR no NAT) -> all subnets to private[0]
  #   per-AZ private RTs                        -> subnet[i] to private[i]
  route_table_id = aws_route_table.private[
    local.private_route_table_count == 1 ? 0 : count.index
  ].id
}
