###############################################################################
# infra/terraform/modules/network/locals.tf
#
# Module-internal locals derived from input variables. Centralized here so
# count-driven resources (NAT gateways, route tables) reference a single
# expression rather than duplicating the conditional logic across
# main.tf and vpc_endpoints.tf.
#
# Locals exposed:
#   - az_count                   : convenience alias for length(var.availability_zones)
#   - nat_gateway_count          : 0 / 1 / az_count depending on tunables
#   - private_route_table_count  : 1 / az_count depending on tunables
#   - module_tags                : tags merged with module-specific Component label
#
# Locals here are pure functions of input variables (no resource references,
# no data sources). This means terraform plan evaluates them at parse time
# without an AWS API call, which gives fast feedback for input validation.
###############################################################################

locals {
  #############################################################################
  # AZ count derived from input variable.
  #
  # Used by:
  #   - aws_subnet.public                    (count = local.az_count)
  #   - aws_subnet.private                   (count = local.az_count)
  #   - aws_route_table_association.public   (count = local.az_count)
  #   - aws_route_table_association.private  (count = local.az_count)
  #############################################################################
  az_count = length(var.availability_zones)

  #############################################################################
  # NAT gateway count.
  #
  # When NAT is disabled (var.enable_nat_gateway = false), no NAT gateways
  # are created. When the cost-optimized single_nat_gateway flag is true,
  # exactly one NAT gateway is shared across AZs (acceptable for dev). For
  # production HA, one NAT gateway per AZ is created.
  #
  # Used by:
  #   - aws_eip.nat
  #   - aws_nat_gateway.main
  #   - aws_route.private_nat
  #############################################################################
  nat_gateway_count = (
    var.enable_nat_gateway
    ? (var.single_nat_gateway ? 1 : local.az_count)
    : 0
  )

  #############################################################################
  # Private route table count.
  #
  # When NAT is disabled OR single_nat_gateway is true, a single private
  # route table is shared across all private subnets. When per-AZ NAT is
  # enabled, each AZ has its own route table so traffic stays AZ-local
  # (each AZ's private subnet routes through that AZ's NAT gateway).
  #
  # Used by:
  #   - aws_route_table.private
  #
  # Always at least 1 because private subnets need a route table even
  # without NAT (the route table simply has no 0.0.0.0/0 route in that
  # case; intra-VPC traffic still works via the implicit local route).
  #############################################################################
  private_route_table_count = (
    var.enable_nat_gateway
    ? (var.single_nat_gateway ? 1 : local.az_count)
    : 1
  )

  #############################################################################
  # Tag merge: caller-provided var.tags + module-specific Component label.
  #
  # Per the folder spec authoring convention: "All resources accept and
  # merge var.tags". Here we layer in Component = "Network" so cost
  # explorer breakdowns can isolate network-related spend. We also layer
  # in Environment = var.environment so this module can be used standalone
  # in tests without relying on the parent composition's provider-level
  # default_tags block.
  #
  # Resources within this module compose further per-resource tags via:
  #   merge(local.module_tags, { Name = "...", Tier = "..." })
  #############################################################################
  module_tags = merge(
    var.tags,
    {
      Component   = "Network"
      Environment = var.environment
    },
  )
}
